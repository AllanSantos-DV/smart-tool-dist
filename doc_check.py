"""Function docstrings: which functions a file has, whether each one is documented, and which ones an edit touched.
Python is read with ast; TypeScript/JavaScript and Java with the code graph analyzers, where the documentation is the
/** */ block right above the function. Used by the edit hook (missing or possibly stale docstrings), the docstring
coverage listing and the one-line summaries in graph and impact results."""
import ast
import difflib
import hashlib
import os
import re
import threading
from collections import Counter, OrderedDict, defaultdict

import code_graph
import index_profile
import index_scope

PYTHON = (".py",)
SCRIPT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
JAVA = (".java",)
SUPPORTED = PYTHON + SCRIPT + JAVA
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

    def visit(node, owner, public, nested):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(child)
                returns = f" -> {ast.unparse(child.returns)}" if child.returns else ""
                found.append({"name": f"{owner}{child.name}", "start": child.lineno, "end": child.end_lineno,
                              "documented": doc is not None, "doc": _first_line(doc or ""),
                              "signature": f"{child.name}({ast.unparse(child.args)}){returns}",
                              "required": public and not nested and _public_name(child.name)
                                          and not any(ast.unparse(d).split(".")[-1] == "override"
                                                      for d in child.decorator_list)})
                visit(child, f"{owner}{child.name}.", False, True)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{owner}{child.name}.", public and _public_name(child.name), nested)
    visit(tree, "", True, False)
    return found


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
    lines = source.splitlines()
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


def functions(path, source, symbols=None):
    """Functions of one file with start/end lines, documented flag, first doc line and normalized signature. symbols
    are the code graph symbols of that file when already known; otherwise the analyzer runs on source."""
    lower = path.lower()
    if lower.endswith(PYTHON):
        try:
            return _python_functions(source)
        except SyntaxError:
            return []
    if symbols is None:
        symbols = analyze(path, source)[0]
    return _braced_functions(path, source, symbols)


def analyze(path, *sources):
    """Analyzer symbols of several versions of one TypeScript/JavaScript/Java file, from one analyzer run for the
    versions not seen yet (an edit hook needs the file before and after; the next edit's 'before' is this 'after')."""
    lower, name = path.lower(), os.path.basename(path)
    keys = [hashlib.sha1(f"{name}\0{source}".encode("utf-8")).hexdigest() for source in sources]
    with _PARSED_LOCK:
        missing = {key: source for key, source in zip(keys, sources) if key not in _PARSED}
    if missing:
        batch = {f"v{i}/{name}": source for i, source in enumerate(missing.values())}
        analysis = (code_graph._javascript_graph(batch) if lower.endswith(SCRIPT)
                    else code_graph._java_graph(batch) if lower.endswith(JAVA) else {})
        found = {key: [dict(s, path=name) for s in analysis.get("symbols") or [] if s.get("path") == f"v{i}/{name}"]
                 for i, key in enumerate(missing)}
        with _PARSED_LOCK:
            _PARSED.update(found)
            while len(_PARSED) > MAX_PARSED:
                _PARSED.popitem(last=False)
    with _PARSED_LOCK:
        return [_PARSED[key] if key in _PARSED else [] for key in keys]


def _touched_lines(before, after):
    old, new = (before or "").splitlines(), after.splitlines()
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
    data = code_graph.build(root, view_id)
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
        missing += [{"path": path, "function": f["name"], "line": f["start"]} for f in found if not f["documented"]]
    per_file = Counter(item["path"] for item in missing)
    return {"functions": total, "documented": total - len(missing),
            "coverage_percent": round(100 * (total - len(missing)) / total, 1) if total else None,
            "missing_total": len(missing), "missing": missing[:limit],
            "files_with_most_missing": [{"path": p, "missing": n} for p, n in per_file.most_common(10)],
            "include_tests": include_tests,
            "diagnostics": [d.get("reason") for d in data.get("diagnostics") or [] if d.get("reason")],
            "view": (data.get("selected") or {}).get("label")}
