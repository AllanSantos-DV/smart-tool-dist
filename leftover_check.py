"""Leftovers an edit writes, for the edit hook: git conflict markers, focused tests, skipped or disabled tests and debugger
lines. Only the lines an edit adds or changes are read. The test and debugger shapes are tied to the files of their language
(a `breakpoint()` in a JavaScript file is not a leftover), a line that is a comment is skipped, and a `=======` line counts
only inside a conflict block that is still open, because it is also the underline of a Markdown or reStructuredText heading.
The source text of a line is never repeated in a note.
"""
import re

import doc_check

_PYTHON = (".py",)
_SCRIPT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_JAVA = (".java", ".kt")
_GO = (".go",)
_COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*)")
_CONFLICT_OPEN = re.compile(r"^<{7}(?:\s|$)")
_CONFLICT_CLOSE = re.compile(r"^>{7}(?:\s|$)")
_CONFLICT_SEPARATOR = re.compile(r"^={7}\s*$")
_RULES = (
    ("focused test", _SCRIPT, re.compile(r"\b(?:it|test|describe|context|suite)\.only\s*\(|(?<![\w.$])(?:fit|fdescribe)\s*\(")),
    ("skipped test", _SCRIPT, re.compile(r"(?<![\w.$])(?:xit|xdescribe)\s*\(")),
    ("skipped test", _PYTHON, re.compile(r"^\s*@pytest\.mark\.(?:skip|xfail)\b")),
    ("skipped test", _JAVA, re.compile(r"^\s*@Disabled\b")),
    ("skipped test", _GO, re.compile(r"\bt\.Skip\(")),
    ("debugger line", _SCRIPT, re.compile(r"^\s*debugger\s*;?\s*$")),
    ("debugger line", _PYTHON, re.compile(r"^\s*(?:breakpoint\(\)|import\s+pdb\b|pdb\.set_trace\(\))")),
)


def _conflict_lines(lines):
    """The 1-based numbers of the lines that are conflict markers of a block, `=======` only between its other two."""
    marks, inside = set(), False
    for number, text in enumerate(lines, 1):
        if _CONFLICT_OPEN.match(text):
            marks.add(number)
            inside = True
        elif _CONFLICT_CLOSE.match(text):
            marks.add(number)
            inside = False
        elif inside and _CONFLICT_SEPARATOR.match(text):
            marks.add(number)
    return marks


def review(rel, before, after):
    """Leftovers among the lines an edit adds or changes.

    Parameters: rel (str) file path, whose suffix picks the language of the test and debugger shapes; before (str or None)
    text before the edit; after (str) text after it.
    Returns: list of dicts with line (1-based) and kind ("conflict marker", "focused test", "skipped test" or "debugger line"),
    in line order, one per line; the text of the line is not included.

    Technical doc: docs/scope/technical/_root.md#leftover_check.py::review
    """
    lines = after.split("\n")
    conflicts = _conflict_lines(lines)
    rules = [(kind, pattern) for kind, suffixes, pattern in _RULES if rel.lower().endswith(suffixes)]
    findings = []
    for number in sorted(doc_check._touched_lines(before, after)):
        if number > len(lines):
            continue
        text = lines[number - 1]
        if number in conflicts:
            findings.append({"line": number, "kind": "conflict marker"})
        elif not _COMMENT.match(text):
            kind = next((kind for kind, pattern in rules if pattern.search(text)), None)
            if kind:
                findings.append({"line": number, "kind": kind})
    return findings
