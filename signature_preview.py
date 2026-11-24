"""What a new parameter list would break, asked before the edit: the call sites of a function whose arguments cannot bind to the
parameters proposed for it.

The call sites are those the code graph holds for the function (resolved calls, calls it left unresolved under that name,
and the calls inside the defining file), as `code_impact` and `removed_callers` read them for a deletion. Each site is
read as text: Python through `ast`, the other languages through a token count of the call's arguments that gives no
verdict when it is unsure (a spread argument, generics, a call it cannot find on the line). Nothing is edited or written.
"""
import ast
import os
import re

import code_graph
import code_impact
import doc_check
import move_preview
import removed_callers

DEFAULT_LIMIT = 30
TEST_DEPTH = 3
MAX_SCAN = 6000
SHOWN_CALL = 120

RULE = ("A call site is `unbindable` when its arguments cannot bind to the proposed parameters: Python by binding the "
        "positional and keyword arguments to the parameter list as the interpreter does (defaults, keyword-only, `*args`, "
        "`**kwargs`; the first parameter of a method is its receiver); other languages by the number of arguments against "
        "the fewest and the most the list takes (a default value, a `?` or a rest parameter makes one optional). A site the "
        "check cannot decide is counted in `unsure` and never reported: a call with a spread or star argument, a call "
        "with generics or an empty argument, a call written through the class name instead of an instance, a call not found "
        "on its line. `resolved` is false when the graph tied the call to the name only, so another function of the same name "
        "may be the callee. Static analysis: a call through a callback, reflection or a name built at run time is not a "
        "call site here.")
_STRING = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`")
_OPTIONAL = re.compile(r"^[\w$]+\s*\?|(?<![=!<>])=(?![=>])")


def _unbalanced(params):
    return ValueError(f"params {params!r} has unbalanced brackets or quotes: pass the parameters as they are written between "
                      "the parentheses.")


def _python_spec(params, implicit):
    """The binding facts of a proposed Python parameter list.

    Parameters: params (str) the list as written between the parentheses; implicit (bool) its first parameter is bound by
    the receiver of a method call and is not a call argument.
    Returns: dict {"slots": names that take a positional argument, in order, "keywords": names a keyword argument may bind,
    "required": names that must be given, "vararg": bool, "kwarg": bool}.
    Raises: ValueError when `params` is not a Python parameter list.
    """
    try:
        arguments = ast.parse(f"def _f({params}):\n    pass\n").body[0].args
    except SyntaxError as error:
        raise ValueError(f"params {params!r} is not a Python parameter list: {error.msg}.") from None
    positional = [item.arg for item in arguments.posonlyargs + arguments.args]
    required = positional[:len(positional) - len(arguments.defaults)]
    required += [item.arg for item, default in zip(arguments.kwonlyargs, arguments.kw_defaults) if default is None]
    keywords = {item.arg for item in arguments.args + arguments.kwonlyargs}
    if implicit and positional:
        receiver = positional.pop(0)
        keywords.discard(receiver)
        required = [name for name in required if name != receiver]
    return {"slots": positional, "keywords": keywords, "required": required, "vararg": arguments.vararg is not None,
            "kwarg": arguments.kwarg is not None}


def _python_reason(spec, positional, keywords):
    """Why a call with this many positional arguments and these keywords cannot bind, None when it can."""
    if positional > len(spec["slots"]) and not spec["vararg"]:
        return f"passes {positional} positional argument(s) and the new parameters take at most {len(spec['slots'])}"
    given = set(spec["slots"][:positional])
    for name in keywords:
        if name in spec["keywords"]:
            if name in given:
                return f"passes {name} both by position and by keyword"
            given.add(name)
        elif not spec["kwarg"]:
            return f"passes the keyword {name}, which no new parameter has"
    missing = [name for name in spec["required"] if name not in given]
    return f"does not give the required parameter(s) {', '.join(missing)}" if missing else None


def _string_end(text, start):
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\" and quote != "`":
            index += 2
        elif text[index] == quote:
            return index + 1
        else:
            index += 1
    return None


def _arguments(text, angles=False):
    """The top-level comma-separated pieces of what `text` holds up to the bracket that closes it.

    Parameters: text (str) the characters after an opening parenthesis; angles (bool) `<` and `>` also nest, as in the
    generic types of a parameter list.
    Returns: tuple (list of str pieces, int index of the closing parenthesis) or None when no closing parenthesis follows
    or a string or comment is left open.
    """
    pieces, current, depth, index = [], [], 0, 0
    while index < len(text):
        char, pair = text[index], text[index:index + 2]
        if char in "'\"`":
            end = _string_end(text, index)
            if end is None:
                return None
            current.append(text[index:end])
            index = end
            continue
        if pair in ("//", "/*"):
            end = text.find("\n" if pair == "//" else "*/", index + 2)
            if end < 0:
                return None
            index = end + (2 if pair == "/*" else 0)
            continue
        if char in "([{" or (angles and char == "<"):
            depth += 1
        elif char in ")]}" or (angles and char == ">" and depth and text[index - 1:index] not in "=-"):
            if depth == 0:
                return (pieces + ["".join(current)], index) if char == ")" else None
            depth -= 1
        elif char == "," and depth == 0:
            pieces.append("".join(current))
            current, index = [], index + 1
            continue
        current.append(char)
        index += 1
    return None


def _pieces(raw):
    """The trimmed pieces of an argument or parameter list; None when a piece in the middle is empty."""
    pieces = [piece.strip() for piece in raw]
    if pieces == [""]:
        return []
    if len(pieces) > 1 and not pieces[-1]:
        pieces.pop()
    return None if any(not piece for piece in pieces) else pieces


def _token_spec(params):
    """The fewest and the most arguments a proposed parameter list of a language other than Python takes.

    Returns: dict {"min": int, "max": int or None (no upper bound)}.
    Raises: ValueError when the brackets or quotes of `params` do not balance.
    """
    scanned = _arguments(params + ")", angles=True)
    if scanned is None or scanned[1] != len(params):
        raise _unbalanced(params)
    pieces = _pieces(scanned[0])
    if pieces is None:
        raise ValueError(f"params {params!r} has an empty parameter between two commas.")
    if pieces and re.match(r"this\s*:", pieces[0]):
        pieces = pieces[1:]
    rest = any("..." in piece for piece in pieces)
    required = [piece for piece in pieces if "..." not in piece and not _OPTIONAL.search(piece)]
    return {"min": len(required), "max": None if rest else len(pieces)}


def _token_reason(spec, count):
    if count < spec["min"]:
        return f"passes {count} argument(s) and the new parameters need at least {spec['min']}"
    if spec["max"] is not None and count > spec["max"]:
        return f"passes {count} argument(s) and the new parameters take at most {spec['max']}"
    return None


def _call_text(text):
    return " ".join(text.split())[:SHOWN_CALL]


def _python_site(source, tree, line, short, bound_to):
    """The (call text, positional count, keyword names) of the one call to `short` that starts on `line`, None when the
    check cannot decide: no such call, several, a spread argument, or a method called through its class (`bound_to` is the
    class name of a method whose receiver is implicit)."""
    found = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and node.lineno == line
             and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute)
                  else None) == short]
    if len(found) != 1:
        return None
    node = found[0]
    if any(isinstance(arg, ast.Starred) for arg in node.args) or any(item.arg is None for item in node.keywords):
        return None
    if bound_to and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
            and node.func.value.id == bound_to:
        return None
    return _call_text(ast.get_source_segment(source, node) or short), len(node.args), [item.arg for item in node.keywords]


def _token_site(lines, line, short):
    """The (call text, argument count) of the one call to `short` that starts on `line`, None when the check cannot decide."""
    if not 1 <= line <= len(lines):
        return None
    found = list(re.finditer(rf"(?<![\w$]){re.escape(short)}\s*\(", lines[line - 1]))
    if len(found) != 1:
        return None
    scanned = _arguments("\n".join(lines[line - 1:])[found[0].end():][:MAX_SCAN])
    pieces = _pieces(scanned[0]) if scanned else None
    if pieces is None or any("..." in plain or "<" in plain for plain in (_STRING.sub('""', piece) for piece in pieces)):
        return None
    return _call_text(lines[line - 1][found[0].start():]), len(pieces)


def _read(root, path):
    try:
        with open(os.path.join(root, *path.split("/")), encoding="utf-8") as stream:
            return stream.read()
    except UnicodeDecodeError as error:
        raise ValueError(f"path {path!r} is not UTF-8 text: {error}") from error


def _function(root, path, symbol, data):
    """The one definition of `symbol` in `path`, from the file as it is now and the graph symbols `data` holds for it.

    Raises: ValueError when the language is not read, nothing, or more than one definition (overloads) carries the name.
    """
    if not doc_check.supported(path):
        raise ValueError(f"path {path!r} is not a language Smart Tool reads; signature_preview takes Python, JavaScript, "
                         "TypeScript, Java and Go files.")
    found = doc_check.functions(path, _read(root, path), [s for s in data.get("symbols") or [] if s["path"] == path])
    bare = [item for item in found if item["name"].split("(")[0] == symbol]
    chosen = bare or [item for item in found if item["name"].split("(")[0].rpartition(".")[2] == symbol]
    if not chosen and any(s["path"] == path and s["name"] == symbol and s.get("kind") == "class"
                          for s in data.get("symbols") or []):
        raise ValueError(f"{symbol!r} is a class: pass the function or Class.method whose parameters change.")
    if not chosen:
        raise ValueError(f"No function or method named {symbol!r} in {path}: pass a function or Class.method that it defines.")
    if len(chosen) > 1:
        raise ValueError(f"{symbol!r} is defined {len(chosen)} times in {path} (overloads or a redefinition): signature_preview "
                         "cannot tell which one the calls bind to.")
    return chosen[0]


def _parsed(root, path, python):
    """A caller file ready to be read: (text, syntax tree) for Python, (text, lines) for the other languages; None when it
    cannot be read or the language is not the function's."""
    if python != path.lower().endswith(".py"):
        return None
    try:
        text = _read(root, path)
        return text, ast.parse(text) if python else text.splitlines()
    except (OSError, ValueError, SyntaxError):
        return None


def _verdict(parsed, line, short, spec, bound_to):
    """{"call", "arguments", "reason"} for the call on `line` of a parsed file (reason None when it binds), None when the
    check cannot decide."""
    text, body = parsed
    if isinstance(body, ast.AST):
        site = _python_site(text, body, line, short, bound_to)
        if site is None:
            return None
        given = [f"{site[1]} positional"] * bool(site[1]) + [f"keyword {', '.join(site[2])}"] * bool(site[2])
        return {"call": site[0], "arguments": ", ".join(given) or "none", "reason": _python_reason(spec, site[1], site[2])}
    site = _token_site(body, line, short)
    if site is None:
        return None
    return {"call": site[0], "arguments": f"{site[1]} positional" if site[1] else "none", "reason": _token_reason(spec, site[1])}


def _callers(root, path, symbol, short, method, view_id, data):
    """The call sites of the function: (set of (path, line) the graph resolved, set it matched by name only, the answer of
    `code_impact.impact`)."""
    found = code_impact.impact(root, f"{path}::{symbol}", view_id, TEST_DEPTH, removed_callers.MAX_SITES)
    definitions = [item for item in found["definitions"] if item["path"] == path]
    if not definitions:
        raise ValueError(f"No function or class named {symbol!r} in {path}; it is defined in "
                         f"{', '.join(sorted({item['path'] for item in found['definitions']}))}.")
    stem = os.path.splitext(os.path.basename(path))[0]
    resolved = {(site["path"], site["line"]) for site in found["callers"]}
    by_name = set(removed_callers._sites(root, data, path, symbol, short, "", removed=False))
    by_name |= {(call["path"], call["line"]) for call in data.get("unresolved") or []
                if call["path"] == path and removed_callers._names(call["expression"], short, stem, method)}
    return resolved, by_name - resolved, found


def preview(root, path, symbol, params, view_id=None, limit=DEFAULT_LIMIT):
    """The call sites that the proposed parameter list of a function would leave unable to bind.

    Parameters: root (str) project folder; path (str) file relative to it that defines the function; symbol (str) the
    function or `Class.method`; params (str) the proposed parameter list as written between the parentheses (a method lists
    its receiver first; `""` for none); view_id (str or None) index view; limit (int) rows returned.
    Returns: dict {"path", "symbol", "current": str the signature now, "proposed": str, "unbindable": [{"path", "line",
    "call": str, "arguments": str, "reason": str, "resolved": bool}], "counts": {"callers", "checked", "unsure",
    "unbindable"}, "summary": str, "note": str, "rule": str}.
    Raises: ValueError when `path` is missing or outside the project, `symbol` is not a function defined once in `path`,
    `params` is not text or not a valid parameter list; RuntimeError when the code graph cannot be built.
    """
    if not isinstance(params, str):
        raise ValueError("Pass params: the proposed parameter list as text, as written between the parentheses (\"\" for none).")
    if not isinstance(symbol, str) or not symbol.strip() or "::" in symbol:
        raise ValueError("symbol must be the name of a function or Class.method defined in path.")
    path = move_preview._relative(path, "path")
    if not move_preview._inside(root, path):
        raise ValueError(f"path {path!r} resolves outside the project folder.")
    if not os.path.isfile(os.path.join(root, *path.split("/"))):
        raise ValueError(f"path {path!r} does not exist in the project or is not a file.")
    symbol, params = symbol.strip(), params.strip()
    data = code_graph.build_current(root, view_id, shared=True)
    function = _function(root, path, symbol, data)
    python = path.lower().endswith(".py")
    qualified = function["name"].split("(")[0]
    short, owner = qualified.rpartition(".")[2], qualified.rpartition(".")[0].rpartition(".")[2] or None
    decorators = function.get("decorators", ())
    implicit = python and owner is not None and "staticmethod" not in decorators
    spec = _python_spec(params, implicit) if python else _token_spec(params)
    bound_to = owner if implicit and "classmethod" not in decorators else None
    resolved, by_name, found = _callers(root, path, symbol, short, owner is not None, view_id, data)
    sites = sorted(resolved | by_name)
    texts, rows, unsure = {}, [], 0
    for site_path, line in sites:
        if site_path not in texts:
            texts[site_path] = _parsed(root, site_path, python)
        verdict = _verdict(texts[site_path], line, short, spec, bound_to) if texts[site_path] else None
        if verdict is None:
            unsure += 1
        elif verdict["reason"]:
            rows.append({"path": site_path, "line": line, **verdict, "resolved": (site_path, line) in resolved})
    counts = {"callers": len(sites), "checked": len(sites) - unsure, "unsure": unsure, "unbindable": len(rows)}
    summary = (f"{counts['unbindable']} of {counts['callers']} call site(s) of {path}::{symbol} cannot bind to ({params})"
               + (f"; {unsure} could not be decided" if unsure else "") + ".")
    cut = found["counts"]["callers"] - len(found["callers"])
    note = " ".join(part for part in (removed_callers._note(data), code_impact.NOTE,
                                      f"{cut} resolved caller(s) beyond the first {removed_callers.MAX_SITES} were not read."
                                      if cut > 0 else "") if part)
    return {"path": path, "symbol": symbol, "current": function["signature"], "proposed": params,
            "unbindable": rows[:limit], "counts": counts, "summary": summary, "note": note, "rule": RULE}
