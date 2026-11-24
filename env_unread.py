"""Environment variables a README or `docs` file names that no code reads: the reverse of `undocumented_env`. The names come
from the forms that only mean an environment variable (`export`, `set`, `$env:`, an assignment before a command, a
`process.env`, `os.environ` or `getenv` form) and from upper-case snake_case names in inline code on a line that says
env or environment; the reads are the literal reads `undocumented_env` collects. Nothing is written.
"""
import re

import code_health
import project_scan
import rename_preview
import undocumented_env

DEFAULT_LIMIT = 30
NOTE = ("Names are read from the git-tracked README and `docs` text (the generated docs/scope is left out): `export` and "
        "`set` assignments, `$env:`, an assignment before a command or inside inline code, `process.env`, `os.environ` and "
        "`getenv` forms, a line of an env, dotenv, ini or properties block, and an upper-case snake_case name in inline "
        "code on a line that says env or environment; names are upper case, three characters or more, and example "
        "placeholders are left out. A variable counts as read when code or a test reads it with a literal name, as "
        "`undocumented_env` sees it: a name built at run time, or read by a library or a script, is not seen, so `elsewhere` "
        "gives the first other tracked file, Markdown and text aside, that holds the name as a word, and the names found in "
        "no other file come first.")

_NAME = r"[A-Z][A-Z0-9_]{2,}"
_FENCE = re.compile(r"^\s*(?:```|~~~)\s*([\w-]*)")
_SPAN = re.compile(r"`([^`\n]+)`")
_STRONG = (re.compile(rf"\bexport\s+({_NAME})="), re.compile(rf"\bset\s+({_NAME})="),
           re.compile(rf"\$env:({_NAME})\b"), re.compile(rf"process\.env\.({_NAME})\b"),
           re.compile(rf"process\.env\[\s*['\"]({_NAME})['\"]"),
           re.compile(rf"os\.environ(?:\.get)?\s*[\[(]\s*['\"]({_NAME})['\"]"),
           re.compile(rf"\bgetenv\s*\(\s*['\"]({_NAME})['\"]"))
_BEFORE_COMMAND = re.compile(rf"^\s*(?:\$\s+)?({_NAME})=\S*\s+\S")
_LINE_START = re.compile(rf"^\s*({_NAME})=")
_SPAN_ASSIGNMENT = re.compile(rf"^({_NAME})=")
_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_ENV_WORD = re.compile(r"\benv\b|environment", re.I)
_PLACEHOLDER = re.compile(r"^(?:YOUR|MY)_|EXAMPLE|PLACEHOLDER|_HERE$|^XXX")
_ENV_BLOCKS = {"env", "dotenv", "ini", "properties"}
_PROSE = (".md", ".rst", ".txt")
_UPPER = re.compile(rf"\b{_NAME}\b")


def _line_names(line, fence):
    """The variable names one doc line names; `fence` is the label of the fenced block it sits in, None outside one."""
    names = [found[1] for pattern in _STRONG for found in pattern.finditer(line)]
    if fence is None:
        for span in (found[1] for found in _SPAN.finditer(line)):
            assignment = _SPAN_ASSIGNMENT.match(span)
            if assignment:
                names.append(assignment[1])
            elif _SNAKE.match(span) and _ENV_WORD.search(line):
                names.append(span)
    else:
        for pattern in (_BEFORE_COMMAND, _LINE_START) if fence in _ENV_BLOCKS else (_BEFORE_COMMAND,):
            found = pattern.match(line)
            if found:
                names.append(found[1])
    return {name for name in names if not _PLACEHOLDER.search(name)}


def _doc_names(text):
    """The (name, line) of every variable a doc names, in line order."""
    fence, found = None, []
    for number, line in enumerate(text.splitlines(), 1):
        marker = _FENCE.match(line)
        if marker:
            fence = None if fence is not None else marker[1].lower() or ""
            continue
        found += [(name, number) for name in sorted(_line_names(line, fence))]
    return found


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The environment variables the README and `docs` files name that no code reads.

    Parameters: root (str) project folder, a git repository; view_id (str or None) index view; limit (int) variables returned.
    Returns: dict {"variables": [{"name", "path", "line", "mentions", "elsewhere"}] with the names found in no other file
    first, then by file and line; `path` and `line` are the first doc site, `mentions` counts the doc sites, `elsewhere` is
    the first other tracked file (not Markdown or text) holding the name as a word, or None, "counts": {"named", "read",
    "unread", "absent", "docs_checked"}, "note": str}.
    Raises: ValueError when the code graph is empty or git cannot list the folder.
    """
    sites = undocumented_env.read_sites(code_health._model(root, view_id))
    named, elsewhere, checked = {}, {}, 0
    for rel, text in project_scan.tracked_texts(root):
        if text is None:
            continue
        if rename_preview._is_doc(rel):
            checked += 1
            for name, line in _doc_names(text):
                named.setdefault(name, []).append((rel, line))
        elif not rel.lower().endswith(_PROSE):
            for name in _UPPER.findall(text):
                elsewhere.setdefault(name, rel)
    rows = []
    for name, places in named.items():
        if name not in sites:
            path, line = min(places)
            rows.append({"name": name, "path": path, "line": line, "mentions": len(places),
                         "elsewhere": elsewhere.get(name)})
    rows.sort(key=lambda row: (row["elsewhere"] is not None, row["path"], row["line"], row["name"]))
    counts = {"named": len(named), "read": len(named) - len(rows), "unread": len(rows),
              "absent": sum(row["elsewhere"] is None for row in rows), "docs_checked": checked}
    return {"variables": rows[:limit], "counts": counts, "note": NOTE}
