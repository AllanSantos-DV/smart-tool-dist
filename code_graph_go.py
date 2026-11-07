"""Go files read without a Go toolchain: imports, the functions, methods and types each file declares, their doc
comments and the static calls between them.

A lexer following the lexical elements of the Go specification (comments, interpreted and raw strings, runes) feeds a
declaration reader built on Go's automatic semicolons: a top-level declaration ends at the first line break, outside
brackets, after a token that ends a statement. A doc comment is the comment group on the lines right above a
declaration, as go/parser attaches it.

A Go package is a directory, so a file that imports a package depends on every non-test file of that directory, and an
internal test file (`package x` in `x_test.go`) depends on every non-test file of its own directory. Import paths are
resolved against the nearest `go.mod` module path; with no `go.mod` in the snapshot an import resolves to the longest
directory whose path ends the import path.

A call is resolved only when its target is certain: a function of the same package, an exported function of an imported
package, or a method called on the receiver, a parameter or a local variable whose type is written in the code
(`var x T`, `x := &T{}`, `x := NewT()` with a declared result type). Calls through interfaces, embedded fields,
function values and chains stay unresolved."""
import bisect
import posixpath
import re
from collections import namedtuple

_TOKEN = re.compile(r"(?P<comment>//[^\n]*|/\*.*?\*/)"
                    r"|(?P<string>\"(?:\\.|[^\"\\\n])*\"|`[^`]*`|'(?:\\.|[^'\\\n])*')"
                    r"|(?P<ident>[^\W\d]\w*)"
                    r"|(?P<number>\.?\d[\w.]*)"
                    r"|(?P<op>:=|\.\.\.|\S)", re.S)
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_PACKAGE = re.compile(r"^package\s+(\w+)", re.M)
_IMPORT_BLOCK = re.compile(r"^import\s*\((.*?)^\)", re.M | re.S)
_IMPORT_ONE = re.compile(r'^import\s+([\w.]+\s+)?"([^"]+)"', re.M)
_IMPORT_SPEC = re.compile(r'^\s*([\w.]+\s+)?"([^"]+)"', re.M)
_MODULE = re.compile(r"^module\s+(\S+)", re.M)
_DIRECTIVE = re.compile(r"^(line |extern |export |[a-z0-9]+:[a-z0-9])")
_VERSION_SUFFIX = re.compile(r"^v\d+$")
_KEYWORDS = {"break", "case", "chan", "const", "continue", "default", "defer", "else", "fallthrough", "for", "func",
             "go", "goto", "if", "import", "interface", "map", "package", "range", "return", "select", "struct",
             "switch", "type", "var"}
_BUILTINS = {"append", "cap", "clear", "close", "complex", "copy", "delete", "imag", "len", "make", "max", "min", "new",
             "panic", "print", "println", "real", "recover", "any", "bool", "byte", "comparable", "complex64",
             "complex128", "error", "float32", "float64", "int", "int8", "int16", "int32", "int64", "rune", "string",
             "uint", "uint8", "uint16", "uint32", "uint64", "uintptr"}
_UNRESOLVED = "External, dynamic or unresolved target in this snapshot."
_Token = namedtuple("_Token", "kind text start end line end_line")


def _lex(text):
    """Code tokens and comments of one Go file, each with its first and last line."""
    newlines = [match.start() for match in re.finditer("\n", text)]
    tokens, comments = [], []
    for match in _TOKEN.finditer(text):
        token = _Token(match.lastgroup, match.group(), match.start(), match.end(),
                       bisect.bisect_left(newlines, match.start()) + 1,
                       bisect.bisect_left(newlines, match.end() - 1) + 1)
        (comments if token.kind == "comment" else tokens).append(token)
    return tokens, comments


def _pairs(tokens):
    """Index of the matching closing bracket of each opening one."""
    pairs, stack = {}, []
    for index, token in enumerate(tokens):
        if token.kind == "op" and token.text in ("(", "[", "{"):
            stack.append(index)
        elif token.kind == "op" and token.text in (")", "]", "}") and stack:
            pairs[stack.pop()] = index
    return pairs


def _ends_statement(token):
    return token.kind in ("ident", "number", "string") or token.text in (")", "]", "}")


def _end(tokens, pairs, start, stop):
    """Index of the last token of the declaration or spec starting at start: where Go inserts a semicolon (a line
    break after a statement-ending token outside brackets), before an explicit ';', or right before stop."""
    index = start
    while index < stop:
        if tokens[index].text in ("(", "[", "{") and tokens[index].kind == "op":
            index = min(pairs.get(index, stop - 1), stop - 1)
        following = tokens[index + 1] if index + 1 < stop else None
        if following is None or following.text == ";" or (following.line > tokens[index].end_line
                                                          and _ends_statement(tokens[index])):
            return index
        index += 1
    return stop - 1


def _comment_text(group):
    """Text of a comment group as go/ast CommentGroup.Text returns it: markers, the first space of a line comment,
    directives (//go:, //line ...) and surrounding empty lines removed."""
    lines = []
    for comment in group:
        if comment.text.startswith("//"):
            body = comment.text[2:]
            if not _DIRECTIVE.match(body):
                lines.append(body[1:] if body.startswith(" ") else body)
        else:
            lines.extend(comment.text[2:-2].splitlines())
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def _lead_docs(tokens, comments):
    """Text of each comment group that stands on its own lines, keyed by the line right after the group. A comment
    after code on the same line is a line comment and never documents the next declaration."""
    code_end = {}
    for token in tokens:
        code_end[token.end_line] = min(code_end.get(token.end_line, token.end), token.end)
    docs, group = {}, []
    for comment in comments:
        trailing = code_end.get(comment.line, comment.start + 1) <= comment.start
        if group and not trailing and comment.line <= group[-1].end_line + 1:
            group.append(comment)
            continue
        if group:
            docs[group[-1].end_line + 1] = _comment_text(group)
        group = [] if trailing else [comment]
    if group:
        docs[group[-1].end_line + 1] = _comment_text(group)
    return docs


def _type_ref(tokens):
    """(package alias or None, type name) of a type written as T, *T, pkg.T or a generic instance of them; None for
    any other type expression."""
    index = 0
    while index < len(tokens) and tokens[index].text in ("*", "&"):
        index += 1
    rest = tokens[index:]
    cut = next((k for k, token in enumerate(rest) if token.text in ("[", "{")), len(rest))
    rest = rest[:cut]
    if len(rest) == 1 and rest[0].kind == "ident" and rest[0].text not in _KEYWORDS:
        return None, rest[0].text
    if len(rest) == 3 and rest[0].kind == "ident" and rest[1].text == "." and rest[2].kind == "ident":
        return rest[0].text, rest[2].text
    return None


def _split(tokens, pairs, start, stop):
    """Comma-separated groups of tokens[start:stop], brackets kept whole."""
    groups, current, index = [], [], start
    while index < stop:
        token = tokens[index]
        if token.text == "," and token.kind == "op":
            groups.append(current)
            current = []
            index += 1
            continue
        close = pairs.get(index, index) if token.text in ("(", "[", "{") and token.kind == "op" else index
        current.extend(tokens[index:close + 1])
        index = close + 1
    if current:
        groups.append(current)
    return groups


def _parameters(tokens, pairs, open_index):
    """Parameter name -> type reference of a parameter list, following Go's grouping (`a, b T`)."""
    found, pending = {}, []
    for group in _split(tokens, pairs, open_index + 1, pairs.get(open_index, open_index)):
        if len(group) == 1 and group[0].kind == "ident":
            pending.append(group[0].text)
            continue
        if len(group) > 1 and group[0].kind == "ident" and group[1].text != ".":
            reference = _type_ref([t for t in group[1:] if t.text != "..."])
            for name in pending + [group[0].text]:
                found[name] = reference
        pending = []
    return found


def _function(tokens, pairs, opens, start, end, text):
    """One func declaration: name, receiver type and variable, lines, signature, parameter and result types and the
    token range of its body (None for a declaration without body)."""
    index = start + 1
    receiver = receiver_var = None
    if index <= end and tokens[index].text == "(":
        close = pairs.get(index, end)
        inner = tokens[index + 1:close]
        cut = next((k for k, token in enumerate(inner) if token.text == "["), len(inner))
        names = [token.text for token in inner[:cut] if token.kind == "ident"]
        if not names:
            return None
        receiver, receiver_var = names[-1], names[0] if len(names) > 1 else None
        index = close + 1
    if index > end or tokens[index].kind != "ident":
        return None
    name = tokens[index].text
    index += 1
    if index <= end and tokens[index].text == "[":
        index = pairs.get(index, end) + 1
    if index > end or tokens[index].text != "(":
        return None
    parameters = _parameters(tokens, pairs, index)
    index = pairs.get(index, end) + 1
    body = None
    opener = opens.get(end)
    if tokens[end].text == "}" and opener is not None and opener >= index and \
            tokens[opener - 1].text not in ("struct", "interface"):
        body = (opener, end)
    results = tokens[index:body[0] if body else end + 1]
    if results and results[0].text == "(":
        groups = _split(tokens, pairs, index + 1, pairs.get(index, index))
        first = groups[0] if groups else []
        if len(first) > 1 and first[0].kind == "ident" and first[1].text != ".":
            first = first[1:]
        result = _type_ref(first)
    else:
        result = _type_ref(results)
    last = tokens[body[0] - 1] if body else tokens[end]
    return {"name": name, "kind": "method" if receiver else "function", "receiver": receiver,
            "receiver_var": receiver_var, "start": tokens[start].line, "end": tokens[end].end_line,
            "signature": " ".join(text[tokens[start].start:last.end].split()), "parameters": parameters,
            "result": result, "body": body}


def _ranges(tokens, pairs, open_index, close):
    """(first, last) token index of each spec inside the brackets at open_index: a grouped declaration or the fields
    of a struct."""
    ranges, spec = [], open_index + 1
    while spec < close:
        spec_end = _end(tokens, pairs, spec, close)
        if tokens[spec].text != ";":
            ranges.append((spec, spec_end))
        spec = spec_end + 1
    return ranges


def _fields(tokens, pairs, open_index):
    """(field names, or None for an embedded field, type reference) of each field of the struct at open_index."""
    found = []
    for first, last in _ranges(tokens, pairs, open_index, pairs.get(open_index, open_index)):
        spec = tokens[first:last + 1]
        if spec and spec[-1].kind == "string":
            spec = spec[:-1]
        if not spec:
            continue
        if spec[0].text == "*" or len(spec) == 1 or spec[1].text == ".":
            found.append((None, _type_ref(spec)))
            continue
        names, index = [spec[0].text], 1
        while index + 1 < len(spec) and spec[index].text == "," and spec[index + 1].kind == "ident":
            names.append(spec[index + 1].text)
            index += 2
        found.append((names, _type_ref(spec[index:])))
    return found


def _specs(tokens, pairs, start, end, docs, text):
    """The type, const or var specs of one declaration, each with its lines and doc comment; a spec of a parenthesized
    declaration also carries the declaration's own doc comment."""
    keyword = tokens[start].text
    group_doc = docs.get(tokens[start].line)
    index = start + 1
    grouped = index <= end and tokens[index].text == "("
    if grouped:
        ranges = _ranges(tokens, pairs, index, pairs.get(index, end))
    else:
        ranges = [(index, end)] if index <= end else []
    found = []
    for spec, spec_end in ranges:
        if tokens[spec].kind != "ident":
            continue
        own = docs.get(tokens[spec].line) if grouped else group_doc
        item = {"start": tokens[spec].line, "end": tokens[spec_end].end_line, "doc": own, "grouped": grouped,
                "group_doc": group_doc if grouped else None, "block": tokens[start].line if grouped else None}
        if keyword == "type":
            after = spec + 1
            if after <= spec_end and tokens[after].text == "[" and after + 1 <= spec_end \
                    and tokens[after + 1].kind == "ident" and tokens[pairs.get(after, after) - 1].kind == "ident":
                after = pairs.get(after, after) + 1
            if after <= spec_end and tokens[after].text == "=":
                after += 1
            head = text[tokens[spec].start:tokens[spec_end].end].split("{")[0]
            struct = after + 1 <= spec_end and tokens[after].text == "struct" and tokens[after + 1].text == "{"
            found.append({**item, "name": tokens[spec].text, "signature": "type " + " ".join(head.split()),
                          "fields": _fields(tokens, pairs, after + 1) if struct else [],
                          "kind": "interface" if after <= spec_end and tokens[after].text == "interface" else "type"})
        else:
            names, index = [tokens[spec].text], spec + 1
            while index + 1 <= spec_end and tokens[index].text == "," and tokens[index + 1].kind == "ident":
                names.append(tokens[index + 1].text)
                index += 2
            found.append({**item, "name": names[0], "names": names, "kind": keyword, "signature": ""})
    return found


def _parse(text):
    """Package name, top-level declarations and the token stream of one Go file."""
    tokens, comments = _lex(text)
    pairs = _pairs(tokens)
    opens = {close: open_ for open_, close in pairs.items()}
    docs = _lead_docs(tokens, comments)
    package, found, index = None, [], 0
    while index < len(tokens):
        token = tokens[index]
        end = _end(tokens, pairs, index, len(tokens))
        if token.kind == "ident" and token.text == "package" and index + 1 < len(tokens):
            package = tokens[index + 1].text
        elif token.kind == "ident" and token.text == "func":
            item = _function(tokens, pairs, opens, index, end, text)
            if item:
                found.append({**item, "doc": docs.get(token.line)})
        elif token.kind == "ident" and token.text in ("type", "const", "var"):
            found.extend(_specs(tokens, pairs, index, end, docs, text))
        index = end + 1
    return package, found, tokens, pairs


def declarations(text):
    """Package name and top-level declarations of one Go file: functions, methods, types, consts and vars, each with
    name, kind, start and end lines, doc comment text (None when there is none) and signature; methods with their
    receiver type, const/var specs with all their names and the line of the parenthesized declaration they sit in
    ("block", None outside one)."""
    return _parse(text)[:2]


def _module_roots(sources):
    """Folder of each go.mod in the snapshot -> its module path."""
    roots = {}
    for path, text in sources.items():
        if posixpath.basename(path) == "go.mod":
            match = _MODULE.search(_COMMENT.sub("", text))
            if match:
                roots[posixpath.dirname(path)] = match.group(1).strip('"')
    return roots


def _enclosing_module(folder, roots):
    """(module folder, module path) of the nearest go.mod above `folder`, or (None, None)."""
    while True:
        if folder in roots:
            return folder, roots[folder]
        if not folder:
            return None, None
        folder = posixpath.dirname(folder)


def _imports(text):
    """(alias or None, import path) of each import of one Go file (comments already blanked)."""
    found = [((match.group(1) or "").strip() or None, match.group(2)) for match in _IMPORT_ONE.finditer(text)]
    for block in _IMPORT_BLOCK.finditer(text):
        found.extend(((spec.group(1) or "").strip() or None, spec.group(2))
                     for spec in _IMPORT_SPEC.finditer(block.group(1)))
    return found


def _default_alias(spec):
    parts = spec.split("/")
    return parts[-2] if len(parts) > 1 and _VERSION_SUFFIX.match(parts[-1]) else parts[-1]


def analyze(sources, limits=None):
    """Imports, symbols (functions, methods, types) and resolved calls of the Go files in sources (path -> text).
    limits caps symbols and edges ({'symbols': n, 'edges': n}); anything left out sets 'truncated'."""
    limits = limits or {}
    max_symbols, max_edges = limits.get("symbols", float("inf")), limits.get("edges", float("inf"))
    go_files = {path: text for path, text in sources.items() if path.endswith(".go")}
    parsed = [path for path in sources if path.endswith(".go") or posixpath.basename(path) == "go.mod"]
    if not go_files:
        return {"parsed": parsed}
    roots = _module_roots(sources)
    packages = {}
    for path in go_files:
        if not path.endswith("_test.go"):
            packages.setdefault(posixpath.dirname(path), []).append(path)

    def package_dir(path, spec):
        folder, module = _enclosing_module(posixpath.dirname(path), roots)
        if module is not None:
            if spec != module and not spec.startswith(module + "/"):
                return None
            return folder if spec == module else posixpath.normpath(posixpath.join(folder, spec[len(module) + 1:]))
        candidates = [d for d in packages if d and (spec == d or spec.endswith("/" + d))]
        return max(candidates, key=len, default=None)

    dependencies, files = [], {}
    for path, text in sorted(go_files.items()):
        bare = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
        imports = []
        for alias, spec in _imports(bare):
            folder = package_dir(path, spec)
            line = bare[:bare.find(f'"{spec}"')].count("\n") + 1
            targets = [t for t in packages.get(folder, ()) if t != path] if folder is not None else []
            imports.append((alias, spec, folder if targets else None))
            if not targets:
                dependencies.append({"source": path, "target": None, "specifier": spec, "line": line, "kind": "import",
                                     "resolution": "external"})
            dependencies.extend({"source": path, "target": target, "specifier": spec, "line": line, "kind": "import",
                                 "resolution": "resolved"} for target in targets)
        clause = _PACKAGE.search(bare)
        if path.endswith("_test.go") and clause and not clause.group(1).endswith("_test"):
            dependencies.extend({"source": path, "target": target, "specifier": posixpath.dirname(path) or ".",
                                 "line": 1, "kind": "package", "resolution": "resolved"}
                                for target in packages.get(posixpath.dirname(path), ()))
        package, found, tokens, pairs = _parse(text)
        files[path] = {"package": package, "decls": found, "tokens": tokens, "pairs": pairs, "imports": imports}
    graph = _symbols_and_calls(files, max_symbols, max_edges)
    return {"dependencies": dependencies, "parsed": parsed, **graph}




def _symbols_and_calls(files, max_symbols, max_edges):
    """Graph symbols of every function, method and type, then the calls each function body makes."""
    dir_package = {}
    for path, info in files.items():
        if not path.endswith("_test.go") and info["package"]:
            dir_package.setdefault(posixpath.dirname(path), info["package"])

    def key_of(path):
        return posixpath.dirname(path), files[path]["package"]

    imports_of = {}
    for path, info in files.items():
        imports_of[path] = {}
        for alias, spec, folder in info["imports"]:
            if alias != "_":
                target = (folder, dir_package.get(folder)) if folder is not None else None
                imports_of[path][alias or (target[1] if target and target[1] else _default_alias(spec))] = target

    def resolve_type(path, reference):
        if not reference or reference[1] in _BUILTINS:
            return None
        alias, name = reference
        if alias is None:
            return key_of(path), name
        target = imports_of[path].get(alias)
        return (target, name) if target else None

    index, results, symbols, calls, unresolved, truncated = {}, {}, [], [], [], False
    for path, info in files.items():
        tables = index.setdefault(key_of(path), {"funcs": {}, "methods": {}, "types": {}, "fields": {},
                                                 "embedded": {}})
        seen = set()
        for decl in info["decls"]:
            if decl["kind"] not in ("function", "method", "type", "interface"):
                continue
            for names, reference in decl.get("fields", ()):
                found = resolve_type(path, reference)
                if names is None and found:
                    tables["embedded"].setdefault(decl["name"], []).append(found)
                    tables["fields"][(decl["name"], found[1])] = found
                for name in names or ():
                    tables["fields"][(decl["name"], name)] = found
            name = f"{decl['receiver']}.{decl['name']}" if decl["kind"] == "method" else decl["name"]
            symbol_id = f"{path}::{name}"
            if decl["name"] in ("init", "_") or symbol_id in seen:
                symbol_id = f"{symbol_id}@{decl['start']}"
            seen.add(symbol_id)
            if len(symbols) >= max_symbols:
                truncated = True
                continue
            symbol = {"id": symbol_id, "path": path, "name": name, "kind": decl["kind"], "language": "go",
                      "start_line": decl["start"], "end_line": decl["end"]}
            symbols.append(symbol)
            decl["symbol"] = symbol
            if decl.get("result"):
                results[symbol_id] = resolve_type(path, decl["result"])
            if decl["kind"] == "method":
                tables["methods"].setdefault((decl["receiver"], decl["name"]), symbol)
            elif decl["kind"] == "function" and decl["name"] not in ("init", "_"):
                tables["funcs"].setdefault(decl["name"], symbol)
            else:
                tables["types"].setdefault(decl["name"], symbol)

    def member(owner, name, table, depth=0):
        """Method or field `name` of the type `owner`, looked up through its embedded fields as Go promotes them."""
        tables = index.get(owner[0]) if owner and depth < 4 else None
        if not tables:
            return None
        if (owner[1], name) in tables[table]:
            return tables[table][(owner[1], name)]
        return next((hit for inner in tables["embedded"].get(owner[1], ())
                     if (hit := member(inner, name, table, depth + 1)) is not None), None)

    for path, info in files.items():
        for decl in info["decls"]:
            if decl["kind"] in ("function", "method") and decl.get("body") and decl.get("symbol"):
                for call in _body_calls(path, info["tokens"], decl, index, imports_of[path], key_of(path), results,
                                        resolve_type, member):
                    if call["kind"] == "reference":
                        if len(calls) < max_edges:
                            calls.append({"source": decl["symbol"]["id"], "target": call["target"]["id"],
                                          "line": call["line"], "kind": "reference", "resolution": "static"})
                        else:
                            truncated = True
                        continue
                    if call["target"] is None:
                        if len(unresolved) < 1000:
                            unresolved.append({"path": path, "line": call["line"], "expression": call["expression"],
                                               "reason": _UNRESOLVED})
                    elif len(calls) < max_edges:
                        calls.append({"source": decl["symbol"]["id"], "target": call["target"]["id"],
                                      "line": call["line"], "kind": "call", "resolution": "static"})
                    else:
                        truncated = True
    return {"symbols": symbols, "calls": calls, "unresolved": unresolved, "truncated": truncated}


def _body_calls(path, tokens, decl, index, imported, own, results, resolve_type, member):
    """Calls in one function body, {'target': symbol or None, 'expression', 'line', 'kind': 'call'}, and the project
    functions it hands over as values, {'target', 'expression', 'line', 'kind': 'reference'}. Variables get a type from the
    receiver, the parameters, `var x T`, composite literals, known results and field selections, in source order; a
    later declaration of the same name drops it (scopes are not tracked, so a shadowed name stays unresolved)."""
    tables = index[own]
    dots = [index.get(target, {}) for alias, target in imported.items() if alias == "." and target]
    typed = {name: resolve_type(path, reference) for name, reference in decl["parameters"].items()}
    if decl["receiver_var"]:
        typed[decl["receiver_var"]] = (own, decl["receiver"])
    local = set(typed)
    open_, close = decl["body"]

    def value(chain):
        found = typed.get(chain[0])
        for name in chain[1:]:
            found = member(found, name, "fields")
        return found

    def callee(chain, name):
        if not chain:
            return tables["funcs"].get(name) or next((d["funcs"][name] for d in dots if name in d.get("funcs", {})),
                                                     None)
        if len(chain) == 1 and chain[0] not in local and imported.get(chain[0]):
            return index.get(imported[chain[0]], {}).get("funcs", {}).get(name)
        return member(value(chain), name, "methods") if chain[0] in typed else None

    def selection(start):
        chain, end = [tokens[start].text], start + 1
        while end + 1 < close and tokens[end].text == "." and tokens[end + 1].kind == "ident":
            chain.append(tokens[end + 1].text)
            end += 2
        return chain[:-1], chain[-1], end

    def expression_type(start):
        """Type of the expression starting at token start when it is a composite literal, a call with a known
        result or a field selection; None otherwise."""
        start += tokens[start].text == "&"
        if start >= close or tokens[start].kind != "ident":
            return None
        chain, name, end = selection(start)
        if tokens[end].text == "{" and len(chain) <= 1:
            return resolve_type(path, (chain[0] if chain else None, name))
        if tokens[end].text == "(":
            found = callee(chain, name)
            return results.get(found["id"]) if found else None
        return value(chain + [name]) if chain and chain[0] in typed else None

    found = []
    for k in range(open_ + 1, close):
        token = tokens[k]
        if token.kind == "op" and token.text == ":=":
            names, back = [], k - 1
            while back > open_ and tokens[back].kind == "ident":
                names.insert(0, tokens[back].text)
                if tokens[back - 1].text != ",":
                    break
                back -= 2
            local.update(names)
            reference = expression_type(k + 1) if names else None
            for name in names:
                typed.pop(name, None)
            if reference:
                typed[names[0]] = reference
        elif token.kind == "ident" and token.text == "var" and tokens[k + 1].kind == "ident":
            local.add(tokens[k + 1].text)
            stop = k + 2
            while stop < close and tokens[stop].line == token.line and tokens[stop].text != "=":
                stop += 1
            reference = resolve_type(path, _type_ref(tokens[k + 2:stop])) if stop > k + 2 else \
                expression_type(stop + 1) if stop < close and tokens[stop].text == "=" else None
            typed.pop(tokens[k + 1].text, None)
            if reference:
                typed[tokens[k + 1].text] = reference
        elif token.kind == "ident" and tokens[k + 1].text == "(" and token.text not in _KEYWORDS:
            first = k
            while tokens[first - 1].text == "." and tokens[first - 2].kind == "ident":
                first -= 2
            if tokens[first - 1].text == ".":
                found.append({"target": None, "expression": f"….{token.text}", "line": token.line, "kind": "call"})
                continue
            chain = [tokens[i].text for i in range(first, k, 2)]
            if not chain and (token.text in _BUILTINS or token.text in tables["types"] or token.text in local):
                continue
            if len(chain) == 1 and chain[0] not in local and imported.get(chain[0]) \
                    and token.text in index.get(imported[chain[0]], {}).get("types", {}):
                continue
            found.append({"target": callee(chain, token.text), "expression": ".".join(chain + [token.text]),
                          "line": token.line, "kind": "call"})
        elif token.kind == "ident" and token.text not in _KEYWORDS and tokens[k - 1].text != "." \
                and tokens[k + 1].text not in (":", "("):
            chain, name, end = selection(k)
            if tokens[end].text == "(" or (not chain and name in local):
                continue
            target = callee(chain, name)
            if target and target.get("kind") in ("function", "method"):
                found.append({"target": target, "expression": ".".join(chain + [name]), "line": token.line,
                              "kind": "reference"})
    return found
