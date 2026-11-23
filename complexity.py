"""Cyclomatic complexity per function, counted from the branch tokens of its lines in the source.

The function line ranges are those of the code graph; the branches are counted on the tokens of the file (Python) or on the
text with comments, strings and regex literals blanked out (JavaScript, TypeScript, Go and Java), never on a syntax tree,
so the number is an approximation. Nothing is written.
"""
import bisect
import io
import itertools
import os
import re
import tokenize
from collections import Counter

import code_graph
import index_profile
import index_scope

C_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".java")
FUNCTION_KINDS = ("function", "method")
SKIPPED_NAMES = ("callback", "anonymous")
DEFAULT_MIN = 10
SKIPPED_SHOWN = 20

RULE = ("complexity = 1 + the branch tokens inside the function's lines: if, elif, for, while, except, case, and, or and the "
        "conditional expression in Python; if, for, while, case, catch, &&, || and ?? and the ?: operator in JavaScript, "
        "TypeScript, Go and Java. This is an approximation by tokens, not a syntax tree: optional chaining and TypeScript "
        "optional markers are told apart from ?: by their neighbours, a regex literal is found by the character before it, "
        "and a function nested inside another is counted in both. Highest first, then path and line.")
NOTE = ("Complexity here is an approximation by tokens (see rule): use it to find the functions to read, not as a metric "
        "to gate on.")

_PYTHON_BRANCHES = frozenset(("if", "elif", "for", "while", "except", "and", "or"))
_C_BRANCH = re.compile(r"\b(?:if|for|while|case|catch)\b|&&|\|\||\?\??")
_BLANKED = re.compile(
    r'"""(?:.|\n)*?"""'
    r"|//[^\n]*|/\*(?:.|\n)*?\*/"
    r"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`"
    r"|(?:(?<=[(,=:\[!&|?{};+\-*%<>~^])|(?<=return)|(?<=typeof))\s*/(?![/*])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[a-z]*")
_NOT_TERNARY_NEXT = frozenset(":),=;]}>")
_NOT_TERNARY_PREV = frozenset("<,")


def _blank(match):
    return re.sub(r"[^\n]", " ", match.group(0))


def _python_branches(text):
    """Branch tokens of Python source: {line: count}."""
    found, row = Counter(), []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME and token.string in _PYTHON_BRANCHES:
                found[token.start[0]] += 1
            if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                if row and row[0][1] == "case" and row[-1][1] == ":":
                    found[row[0][0]] += 1
                row = []
            elif token.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                row.append((token.start[0], token.string))
    except (tokenize.TokenError, SyntaxError) as error:
        raise ValueError(f"cannot tokenize: {error}") from error
    return dict(found)


def _is_ternary(code, at):
    """Whether the single `?` at `at` of blanked C-like text starts a conditional expression."""
    after = code[at + 1:at + 3].lstrip()
    before = code[:at].rstrip()[-1:]
    if after[:1] == "." and not after[1:2].isdigit():
        return False
    return after[:1] not in _NOT_TERNARY_NEXT and before not in _NOT_TERNARY_PREV and not after.startswith(("extends", "super"))


def _c_branches(text):
    """Branch tokens of JavaScript, TypeScript, Go or Java source with comments and literals blanked: {line: count}."""
    code = _BLANKED.sub(_blank, text)
    starts = [0] + [m.end() for m in re.finditer("\n", code)]
    found = Counter()
    for match in _C_BRANCH.finditer(code):
        if match.group() == "?" and not _is_ternary(code, match.start()):
            continue
        found[bisect.bisect_right(starts, match.start())] += 1
    return dict(found)


def branches_by_line(path, text):
    """The branch tokens of a source file by line.

    Parameters: path (str) file name, whose suffix picks the language; text (str) its content.
    Returns: dict {line (1-based): number of branch tokens that start on it}.
    Raises: ValueError naming the file when the suffix is not Python, JavaScript, TypeScript, Go or Java, or the text does
    not tokenize.
    """
    suffix = os.path.splitext(path)[1].lower()
    try:
        if suffix == ".py":
            return _python_branches(text)
        if suffix in C_SUFFIXES:
            return _c_branches(text)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    raise ValueError(f"{path}: no branch counting for the suffix {suffix or '(none)'}")


def _read(root, path):
    with open(os.path.join(root, path), encoding="utf-8") as stream:
        return stream.read()


def _measured(spans, by_line):
    """The complexity of each (start, end) line range, from the branches by line."""
    last = max([end for _start, end in spans] + list(by_line)) + 1
    cumulative = [0] + list(itertools.accumulate(by_line.get(line, 0) for line in range(1, last + 1)))
    return [1 + cumulative[min(end, last)] - cumulative[start - 1] for start, end in spans]


def report(root, view_id=None, limit=30, min_complexity=DEFAULT_MIN):
    """The functions of the indexed project ranked by cyclomatic complexity.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) functions returned;
    min_complexity (int) lowest complexity listed.
    Returns: dict {"functions": [{"name", "path", "line", "size", "complexity"}] highest first, "counts": {"functions"
    (measured), "matching" (at or above the minimum), "skipped" (files)}, "skipped": [{"path", "reason"}] (files unread,
    untokenizable or in a language without counting, first 20), "min_complexity": int, "rule": str, "graph_note": str
    (what the analysis limits cut, "" when nothing), "note": str}. Functions of test files, unnamed callbacks and
    kinds other than function and method are not measured.
    Raises: ValueError when the code graph is empty.
    """
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    by_path = {}
    for symbol in data["symbols"]:
        if (symbol.get("kind") in FUNCTION_KINDS and symbol["name"].split(".")[-1] not in SKIPPED_NAMES
                and index_profile.kind(symbol["path"], profile) == "code"):
            by_path.setdefault(symbol["path"], []).append(symbol)
    rows, skipped = [], []
    for path, symbols in sorted(by_path.items()):
        try:
            by_line = branches_by_line(path, _read(root, path))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            skipped.append({"path": path, "reason": str(error)})
            continue
        scores = _measured([(s["start_line"], s["end_line"]) for s in symbols], by_line)
        rows += [{"name": s["name"], "path": path, "line": s["start_line"], "size": s["end_line"] - s["start_line"],
                  "complexity": score} for s, score in zip(symbols, scores)]
    matching = sorted((row for row in rows if row["complexity"] >= min_complexity),
                      key=lambda row: (-row["complexity"], row["path"], row["line"]))
    return {"functions": matching[:limit],
            "counts": {"functions": len(rows), "matching": len(matching), "skipped": len(skipped)},
            "skipped": skipped[:SKIPPED_SHOWN],
            "min_complexity": min_complexity,
            "rule": RULE,
            "graph_note": code_graph.left_out_sentence(data) or "",
            "note": NOTE}
