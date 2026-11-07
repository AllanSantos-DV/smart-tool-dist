"""Comments in the wrong place, for the edit hook. Documentation belongs in the language's docstring (see doc_check); a
comment is for a short, non-obvious reason inside a function. Only comments on the lines an edit adds or changes are
checked, in four forms: commented-out code (the ruff ERA001 / eradicate rule), history or ticket tags, a loose comment
block above a function instead of its docstring, and an inline block inside a function longer than MAX_INLINE_LINES."""
import ast
import io
import re
import tokenize

import doc_check

MAX_INLINE_LINES = 2
ISSUES = ("commented_code", "history_tag", "loose_docs", "inline_long")

_DIRECTIVE = re.compile(
    r"^\s*(?:!|-\*-|type:|noqa|pragma|pylint|fmt:|isort|mypy|pyright|ruff|nosec|go:|\+build|nolint|eslint|@ts-|"
    r"prettier|istanbul|c8 |jshint|biome|#region|#endregion|<reference|SPDX-|Copyright|License)", re.I)
_DIVIDER = re.compile(r"[-=*_#~]{3,}")
_TAG = re.compile(
    r"\b[A-Z][A-Z0-9]{1,9}-\d+\b|\b(?:issue|pr|pull request|ticket|pedido|task|bug)\s*#?\d+|\bv\d+(?:\.\d+)+\b"
    r"|\b(?:version|release)\s+\d+(?:\.\d+)+|\b(?:was replaced|were replaced|now uses|changed from|"
    r"added in|fixed in|removed in|new in|formerly|renamed from|before the change|since the rewrite)\b", re.I)
_BRACED_CODE = re.compile(
    r"^(?:(?:if|for|while|switch|catch)\s*\(.*\)\s*\{?$|(?:if|for|while|switch|else)\b.*\{$|(?:else|try|finally)\s*\{?$|"
    r"(?:return|throw|break|continue)\b[^.]*;$|(?:const|let|var|import|export|package|public|private|protected|static|"
    r"func|type)\b.*[;{]$|(?:[\w<>\[\],.?]+\s+)?[\w.$\[\]]+\s*[-+*/|&]?=(?!=).*;$|[\w.$\[\]]+\s*:=.*|[\w.$]+\([^()]*\)\s*;$|[})]+;?$)")
_PYTHON_STATEMENTS = (ast.Assign, ast.AugAssign, ast.Return, ast.Raise, ast.Import, ast.ImportFrom, ast.Delete,
                      ast.Assert, ast.Pass, ast.Break, ast.Continue, ast.Global, ast.Nonlocal, ast.If, ast.For,
                      ast.While, ast.Try, ast.With, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_NEXT_DECLARATION = re.compile(r"^\s*(?:@|(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|def)\b)")


def _python_comments(source):
    """Comment tokens of a Python source: dicts with start, end, text, trailing, doc. Empty when it does not tokenize."""
    found = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                row, column = token.start
                found.append({"start": row, "end": row, "text": token.string[1:], "trailing": bool(token.line[:column].strip()),
                              "doc": False})
    except (tokenize.TokenError, SyntaxError):
        return []
    return found


def _skip_string(source, index, go):
    """Index just after the string literal opening at `index` (quote, template or raw string, Java text block)."""
    quote = source[index]
    if source.startswith('"""', index):
        end = source.find('"""', index + 3)
        return len(source) if end < 0 else end + 3
    cursor = index + 1
    while cursor < len(source):
        char = source[cursor]
        if char == "\\" and not (go and quote == "`"):
            cursor += 2
            continue
        if char == quote:
            return cursor + 1
        if char == "\n" and quote != "`":
            return cursor
        cursor += 1
    return cursor


def _braced_comments(source, go):
    """Comments of a JavaScript, TypeScript, Java or Go source, skipping what is inside string literals."""
    found, index, line, line_start = [], 0, 1, 0
    while index < len(source):
        char = source[index]
        if char == "\n":
            line, index, line_start = line + 1, index + 1, index + 1
        elif source.startswith("//", index):
            end = source.find("\n", index)
            end = len(source) if end < 0 else end
            found.append({"start": line, "end": line, "text": source[index + 2:end],
                          "trailing": bool(source[line_start:index].strip()), "doc": False})
            index = end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            body = source[index:end]
            lines = body.count("\n")
            found.append({"start": line, "end": line + lines, "text": body[2:-2] if body.endswith("*/") else body[2:],
                          "trailing": bool(source[line_start:index].strip()),
                          "doc": body.startswith("/**") and not body.startswith("/**/")})
            line += lines
            index = end
        elif char in "\"'`":
            end = _skip_string(source, index, go)
            line += source.count("\n", index, end)
            index = end
        else:
            index += 1
    return found


def _blocks(comments):
    """Consecutive standalone line comments merged into one block; directives and dividers dropped."""
    blocks = []
    for comment in comments:
        text = comment["text"]
        if comment["doc"] or _DIRECTIVE.match(text) or _DIVIDER.search(text):
            continue
        last = blocks[-1] if blocks else None
        if (last and not last["trailing"] and not comment["trailing"] and last["end"] + 1 == comment["start"]
                and last["end"] == last["start"] + len(last["lines"]) - 1):
            last["end"] = comment["end"]
            last["lines"].append(text.strip())
        else:
            blocks.append({"start": comment["start"], "end": comment["end"], "trailing": comment["trailing"],
                           "lines": [text.strip()] if comment["end"] == comment["start"] else
                           [part.strip(" *") for part in text.splitlines()]})
    return blocks


def _python_code(text):
    """True when the comment text is a Python statement and not prose (ERA001 heuristic)."""
    text = text.strip()
    if len(text.split()) < 2 and "(" not in text and "=" not in text:
        return False
    for candidate in (text, text + " pass" if text.endswith(":") else None):
        if candidate is None:
            continue
        try:
            body = ast.parse(candidate).body
        except (SyntaxError, ValueError):
            continue
        if body and all(_python_statement(node, text) for node in body):
            return True
    return False


def _python_statement(node, text):
    """Whether a parsed node of a comment reads as code: bare keywords like `return value is cached` need punctuation."""
    if isinstance(node, (ast.Return, ast.Raise, ast.Delete, ast.Assert, ast.Global, ast.Nonlocal)):
        return any(char in text for char in "()[]{}=.,'\"")
    if isinstance(node, ast.AnnAssign):
        return node.value is not None
    if isinstance(node, ast.Expr):
        return isinstance(node.value, (ast.Call, ast.Await))
    return isinstance(node, _PYTHON_STATEMENTS)


def _is_code(text, python):
    return _python_code(text) if python else bool(_BRACED_CODE.match(text.strip()))


def _inside(functions, block, lines, go):
    """The function whose body holds the block, or None."""
    for function in functions:
        if go and not lines[function["start"] - 1].lstrip().startswith("func"):
            continue
        if function["start"] < block["start"] and block["end"] <= function["end"]:
            return function
    return None


def _next_line(lines, end):
    """Number and text of the first non-blank line after line `end` (1-based), or (0, "")."""
    for number in range(end + 1, len(lines) + 1):
        if lines[number - 1].strip():
            return number, lines[number - 1]
    return 0, ""


def review(path, before, after, edited=None):
    """Comments the edit from before to after leaves in a wrong place.

    Parameters: path (str) edited file; before (str or None) text before the edit; after (str) text after it; edited
    is `doc_check.written` of the same edit when the caller already has it.
    Returns: list of dicts with line, issue (one of ISSUES), name (enclosing function or None) and text, in line order;
    empty for an unsupported file or one that does not tokenize.
    """
    if not doc_check.supported(path):
        return []
    python = path.lower().endswith(doc_check.PYTHON)
    go = path.lower().endswith(doc_check.GO)
    comments = _python_comments(after) if python else _braced_comments(after, go)
    if not comments:
        return []
    edited = edited or doc_check.written(path, before, after)
    touched = doc_check._touched_lines(before, after)
    lines = after.splitlines()
    declarations = {function["start"] for function in edited["all"]}
    findings = []
    for block in _blocks(comments):
        if not touched.intersection(range(block["start"], block["end"] + 1)):
            continue
        text = " ".join(block["lines"])
        owner = _inside(edited["all"], block, lines, go)
        name = owner["name"] if owner else None
        issue = None
        if _is_code(block["lines"][0], python) or len(block["lines"]) > 1 and _is_code(" ".join(block["lines"]), python):
            issue = "commented_code"
        elif _TAG.search(text):
            issue = "history_tag"
        elif not block["trailing"] and not go and _loose_docs(lines, block, declarations):
            issue = "loose_docs"
        elif owner and block["end"] - block["start"] + 1 > MAX_INLINE_LINES:
            issue = "inline_long"
        if issue:
            findings.append({"line": block["start"], "issue": issue, "name": name, "text": text[:80]})
    return findings


def _loose_docs(lines, block, declarations):
    number, text = _next_line(lines, block["end"])
    return bool(number) and (number in declarations or bool(_NEXT_DECLARATION.match(text)))
