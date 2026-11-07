"""Function docstrings: which functions a file has, whether each one is documented, and which ones an edit touched.
Python is read with ast; TypeScript/JavaScript and Java with the code graph analyzers, where the documentation is the
/** */ block right above the function; Go with the Go reader of the code graph, where the documentation is the doc
comment of each exported declaration (functions, methods, types, consts and vars) and must start with its name. Used by
the edit hook (missing or possibly stale docstrings), the docstring coverage listing and the one-line summaries in
graph and impact results."""
import ast
import difflib
import hashlib
import os
import re
import threading
from collections import Counter, OrderedDict, defaultdict

import code_graph
import code_graph_go
import index_profile
import index_scope

PYTHON = (".py",)
SCRIPT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
JAVA = (".java",)
GO = (".go",)
SUPPORTED = PYTHON + SCRIPT + JAVA + GO
GO_COMMON_METHODS = {"Error", "Read", "ServeHTTP", "String", "Write", "Unwrap"}
GO_SORT_METHODS = {"Len", "Less", "Swap"}
GO_ARTICLES = ("A ", "An ", "The ", "This ")
MAX_PARSED = 64
_SPACE = re.compile(r"\s+")
_PARSED = OrderedDict()
_PARSED_LOCK = threading.Lock()


def supported(path):
    return str(path).lower().endswith(SUPPORTED)


def _first_line(text):
    return next((line.strip(" *") for line in text.strip().splitlines() if line.strip(" *")), "")


def _public_name(name):
    return not name.startswith("_") or name.startswith("__") and name.endswith("__")


def _python_functions(source):
    """Python functions with "required" following pydocstyle publicity (public name, public parents, not nested in a
    function) and PEP 698: an @override method inherits its documentation."""
    tree = ast.parse(source)
    found = []
    imported = _imported_names(tree)

    def visit(node, owner, public, nested, bases=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(child)
                returns = f" -> {ast.unparse(child.returns)}" if child.returns else ""
                found.append({"name": f"{owner}{child.name}", "start": child.lineno, "end": child.end_lineno,
                              "documented": doc is not None, "doc": _first_line(doc or ""),
                              "decorators": [ast.unparse(d) for d in child.decorator_list], "bases": list(bases),
                              "signature": f"{child.name}({ast.unparse(child.args)}){returns}",
                              "required": public and not nested and _public_name(child.name)
                                          and not any(ast.unparse(d).split(".")[-1] == "override"
                                                      for d in child.decorator_list)})
                visit(child, f"{owner}{child.name}.", False, True)
            elif isinstance(child, ast.ClassDef):
                bases = [_dotted(ast.unparse(b), imported) for b in child.bases]
                visit(child, f"{owner}{child.name}.", public and _public_name(child.name), nested, bases)
    visit(tree, "", True, False)
    return found


def _imported_names(tree):
    """Local name -> dotted origin for the imports of a module; a relative import keeps its leading dots."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names[alias.asname or alias.name] = f"{'.' * node.level}{node.module or ''}.{alias.name}"
    return names


def _dotted(expression, imported):
    """Base-class expression with its first name replaced by the module it was imported from."""
    head, _dot, rest = expression.partition(".")
    return imported[head] + (f".{rest}" if rest else "") if head in imported else expression


def _overload_head(lines, index, name):
    """Line where the overload signature ending at index starts, or None when the lines above are not an overload of
    name (a declaration ending in ';')."""
    head = re.compile(rf"^\s*(export\s+)?(default\s+)?(declare\s+)?(public\s+|protected\s+)?(static\s+)?(async\s+)?"
                      rf"(function\s+)?{re.escape(name)}\s*[<(]")
    for k in range(index, max(index - 30, -1), -1):
        line = lines[k].rstrip()
        if k != index and (line.endswith(("*/", "}", ";")) or not line):
            return None
        if head.match(lines[k]):
            return k
    return None


def _block_above(lines, start, name=None):
    """Text of the /** */ block above the function starting at line start. With name (TypeScript/JavaScript), uncommented
    overload signatures in between are skipped, as eslint-plugin-jsdoc does by default."""
    index = start - 2
    while index >= 0:
        if not lines[index].strip() or lines[index].lstrip().startswith("@"):
            index -= 1
            continue
        head = _overload_head(lines, index, name) if name and lines[index].rstrip().endswith(";") else None
        if head is None:
            break
        index = head - 1
    if index < 0 or not lines[index].rstrip().endswith("*/"):
        return None
    end = index
    while index >= 0 and "/*" not in lines[index]:
        index -= 1
    if index < 0 or "/**" not in lines[index]:
        return None
    return "\n".join(line.strip().removeprefix("/**").removesuffix("*/").strip().removeprefix("*").strip()
                     for line in lines[index:end + 1])


def _annotations(lines, start, end):
    """Annotation lines right above the function and at its first lines (the Java analyzer starts a method at its
    annotations), and the first line that is not an annotation."""
    index, found = start - 2, []
    while index >= 0 and (not lines[index].strip() or lines[index].lstrip().startswith("@")):
        found.append(lines[index].strip())
        index -= 1
    index = start - 1
    while index < min(end, len(lines)) and lines[index].lstrip().startswith("@"):
        found.append(lines[index].strip())
        index += 1
    return found, lines[index] if index < len(lines) else ""


def _required(path, symbol, parent, first_line, annotations):
    """Whether a braced-language function needs documentation by the linter defaults: Checkstyle MissingJavadocMethod
    (no @Override, not private) and eslint-plugin-jsdoc require-jsdoc (named declarations and class methods, never
    callbacks or functions nested in another function; private members and unexported arrow functions left out)."""
    short = symbol["name"].split("(")[0].split(".")[-1]
    if short in ("callback", "anonymous") or not _public_name(short) or short.startswith("#"):
        return False
    if re.search(r"\bprivate\b", first_line):
        return False
    if path.lower().endswith(JAVA):
        return not any(a.startswith("@Override") for a in annotations)
    if parent and parent.get("kind") in ("function", "method"):
        return False
    if symbol.get("kind") == "method" or parent and parent.get("kind") in ("class", "interface"):
        return True
    return bool(re.match(r"\s*(export\s+)?(default\s+)?(declare\s+)?(async\s+)?function\b", first_line)
                or re.match(r"\s*export\b", first_line))


def _braced_functions(path, source, symbols):
    lines = source.split("\n")
    by_id = {s.get("id"): s for s in symbols}
    script = path.lower().endswith(SCRIPT)
    found = []
    for symbol in symbols:
        if symbol.get("kind") not in ("function", "method") or "@" in symbol["name"]:
            continue
        start, end = symbol["start_line"], symbol["end_line"]
        short = symbol["name"].split("(")[0].split(".")[-1]
        block = _block_above(lines, start, short if script else None)
        header = " ".join(lines[start - 1:min(end, start + 8)])
        annotations, first_line = _annotations(lines, start, end)
        required = _required(path, symbol, by_id.get(symbol.get("parent")), first_line, annotations)
        found.append({"name": symbol["name"], "start": start, "end": end, "documented": block is not None,
                      "doc": _first_line(block or ""), "signature": _SPACE.sub(" ", header.split("{")[0]).strip(),
                      "required": required})
    return found


def _go_form(doc, name, articles):
    """Whether a Go doc comment starts with the declared name, after an article for a type (revive exported rule)."""
    first = next((line.strip() for line in doc.splitlines() if line.strip()), "")
    if articles:
        first = next((first[len(a):] for a in GO_ARTICLES if first.startswith(a)), first)
    return first.startswith(name + " ")


def _go_functions(path, source):
    """Exported Go declarations as the revive exported rule checks them: "required" outside test files and package
    main, for exported functions, types, consts and vars and the exported methods of exported types (Error, String,
    ServeHTTP and the other common interface methods, and Len/Less/Swap of a sortable type, are left out). A doc
    comment that does not start with the name counts as undocumented ("malformed"). A parenthesized const or var
    block is satisfied by its own doc comment; without one, its first exported spec lacking a comment stands for the
    block, as revive reports it once."""
    package, decls = code_graph_go.declarations(source)
    importable = not path.lower().endswith("_test.go") and package != "main"
    methods = defaultdict(set)
    for decl in decls:
        if decl["kind"] == "method":
            methods[decl["receiver"]].add(decl["name"])
    sortable = {receiver for receiver, names in methods.items() if GO_SORT_METHODS <= names}
    found, reported_blocks = [], set()

    def add(decl, name, short, required, doc, articles=False):
        documented = bool(doc) and _go_form(doc, short, articles)
        found.append({"name": name, "start": decl["start"], "end": decl["end"], "documented": documented,
                      "doc": _first_line(doc or ""), "signature": decl["signature"], "required": required,
                      "kind": decl["kind"], "convention": "go", "malformed": bool(doc) and not documented})

    for decl in decls:
        short = decl["name"]
        if decl["kind"] in ("function", "method"):
            receiver = decl["receiver"]
            required = importable and short[:1].isupper() and (
                not receiver or receiver[:1].isupper() and short not in GO_COMMON_METHODS
                and not (short in GO_SORT_METHODS and receiver in sortable))
            add(decl, f"{receiver}.{short}" if receiver else short, short, required, decl["doc"])
        elif decl["kind"] in ("type", "interface"):
            add(decl, short, short, importable and short[:1].isupper(), decl["doc"] or decl["group_doc"], True)
        else:
            exported = next((n for n in decl["names"] if n[:1].isupper()), None)
            if exported is None:
                continue
            if decl["grouped"] and decl["group_doc"]:
                if decl["block"] not in reported_blocks:
                    reported_blocks.add(decl["block"])
                    found.append({"name": exported, "start": decl["start"], "end": decl["end"], "documented": True,
                                  "doc": _first_line(decl["group_doc"]), "signature": "", "required": importable,
                                  "kind": decl["kind"], "convention": "go", "malformed": False})
                continue
            if decl["grouped"] and not decl["doc"]:
                if decl["block"] in reported_blocks:
                    continue
                reported_blocks.add(decl["block"])
            add(decl, exported, exported, importable, decl["doc"])
    return found


def functions(path, source, symbols=None):
    """Functions of one file with start/end lines, documented flag, first doc line and normalized signature. symbols
    are the code graph symbols of that file when already known; otherwise the analyzer runs on source."""
    lower = path.lower()
    if lower.endswith(PYTHON):
        try:
            return _python_functions(source)
        except SyntaxError:
            return []
    if lower.endswith(GO):
        return _go_functions(path, source)
    if symbols is None:
        symbols = analyze(path, source)[0]
    return _braced_functions(path, source, symbols)


def analyze(path, *sources):
    """Analyzer symbols of several versions of one TypeScript/JavaScript/Java file (an edit hook needs the file before
    and after; the next edit's 'before' is this 'after'). See analyze_files."""
    return analyze_files([(path, source) for source in sources])


def analyze_files(items):
    """Analyzer symbols of each (path, source) pair, in order, from one analyzer run per language for the pairs not
    seen yet: starting the analyzer costs more than reading a file, so callers with many files pass them together.
    An analysis that failed or timed out is returned empty but not cached, so the next call tries again."""
    keys = [hashlib.sha1(f"{os.path.basename(path)}\0{source}".encode("utf-8")).hexdigest() for path, source in items]
    with _PARSED_LOCK:
        missing = {key: item for key, item in zip(keys, items) if key not in _PARSED}
    found = {}
    for extensions, analyzer in ((SCRIPT, code_graph._javascript_graph), (JAVA, code_graph._java_graph)):
        group = [(key, path, source) for key, (path, source) in missing.items() if path.lower().endswith(extensions)]
        if not group:
            continue
        batch = {f"v{i}/{os.path.basename(path)}": source for i, (_key, path, source) in enumerate(group)}
        analysis = analyzer(batch)
        names = list(batch)
        result = {key: [dict(s, path=os.path.basename(path)) for s in analysis.get("symbols") or []
                        if s.get("path") == names[i]] for i, (key, path, _source) in enumerate(group)}
        found.update(result)
        with _PARSED_LOCK:
            if not analysis.get("retryable"):
                _PARSED.update(result)
            while len(_PARSED) > MAX_PARSED:
                _PARSED.popitem(last=False)
    with _PARSED_LOCK:
        cached = {key: _PARSED[key] for key in keys if key in _PARSED}
    return [cached.get(key, found.get(key, [])) for key in keys]


def _touched_lines(before, after):
    old, new = (before or "").split("\n"), after.split("\n")
    touched = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            touched.update(range(j1 + 1, max(j1 + 1, j2) + 1))
    return touched


def written(path, before, after):
    """Functions of the edited file after the edit ('all') and the ones the edit touched ('touched')."""
    if path.lower().endswith(SCRIPT + JAVA):
        analyze(path, *(source for source in (before, after) if source is not None))
    found = functions(path, after) if supported(path) else []
    lines = _touched_lines(before, after)
    return {"all": found, "touched": [f for f in found if lines.intersection(range(f["start"], f["end"] + 1))]}


def review(path, before, after, edited=None):
    """Functions the edit from before to after touched that are left without a docstring although the language
    convention expects one ('missing': public, not nested, not an override) or keep one while their signature changed
    ('signature_changed'). Untouched functions and body-only edits are not reported. edited is written() of the same
    edit when the caller already has it."""
    if not supported(path):
        return []
    edited = edited or written(path, before, after)
    if not edited["touched"]:
        return []
    old = {f["name"]: f for f in functions(path, before)} if before else {}
    findings = []
    for function in edited["touched"]:
        previous = old.get(function["name"])
        if not function["documented"]:
            if function["required"]:
                findings.append({**function, "issue": "missing"})
        elif previous and _SPACE.sub("", previous["signature"]) != _SPACE.sub("", function["signature"]):
            findings.append({**function, "issue": "signature_changed"})
    return findings


def coverage(root, include_tests=False, limit=30, view_id=None):
    """Docstring coverage of the indexed code: functions the language convention expects documented (plus any already
    documented) and those still missing, file by file, so an agent can document a project that started without it.
    Tests are left out unless include_tests."""
    data = code_graph.build_current(root, view_id)
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    kinds = ("code", "test") if include_tests else ("code",)
    by_path = defaultdict(list)
    for symbol in data.get("symbols") or []:
        if supported(symbol["path"]) and index_profile.kind(symbol["path"], profile) in kinds:
            by_path[symbol["path"]].append(symbol)
    total, missing = 0, []
    for path in sorted(by_path):
        try:
            with open(os.path.join(root, path), encoding="utf-8") as stream:
                source = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        found = [f for f in functions(path, source, by_path[path]) if f["required"] or f["documented"]]
        total += len(found)
        missing += [{"path": path, "function": f["name"], "line": f["start"], **({"malformed": True} if f.get("malformed")
                     else {})} for f in found if not f["documented"]]
    per_file = Counter(item["path"] for item in missing)
    return {"functions": total, "documented": total - len(missing),
            "coverage_percent": round(100 * (total - len(missing)) / total, 1) if total else None,
            "missing_total": len(missing), "missing": missing[:limit],
            "files_with_most_missing": [{"path": p, "missing": n} for p, n in per_file.most_common(10)],
            "include_tests": include_tests,
            "diagnostics": [d.get("reason") for d in data.get("diagnostics") or [] if d.get("reason")],
            "view": (data.get("selected") or {}).get("label")}
