"""Environment variables the code reads that no README or `docs` file mentions by name: a literal `os.environ[...]`,
`os.environ.get`, `os.getenv`, `process.env.NAME`, `process.env['NAME']`, a destructuring of `process.env` or a `getenv`
call, found in the code and test files of the index with the scan `code_health.comments` makes, and the README and `docs`
files `doc_refs` reads. Nothing is written.
"""
import re

import code_health
import doc_check
import project_scan
import rename_preview

DEFAULT_LIMIT = 30
NOTE = ("Reads with a literal name in Python, JavaScript/TypeScript, Go and Java files: a name built at run time or read "
        "through a variable is not seen, and a line that only sets or deletes a variable is not a read. A variable counts as "
        "documented when the git-tracked README or `docs` text (the generated docs/scope is left out) writes its name as a "
        "whole word with the same case; `path` and `line` are the first code read, or the first test read when only tests "
        "read it.")

_NAME = r"[A-Za-z_]\w*"
_NOT_WRITE = r"(?!\s*[-+*/|&^%]?=(?!=))"
_GETENV = re.compile(rf"(?<![\w.])(?:(?:os|System)\.)?(?:[gG]etenv|LookupEnv)\s*\(\s*['\"]({_NAME})['\"]")
_PYTHON_IMPORT = re.compile(r"^\s*from\s+os\s+import\s+[^\n]*\benviron\b", re.M)
_NODE = (re.compile(rf"process\.env\.({_NAME})\b{_NOT_WRITE}"),
         re.compile(rf"process\.env\[\s*['\"]({_NAME})['\"]\s*\]{_NOT_WRITE}"))
_DESTRUCTURE = re.compile(r"\{([^{}]*)\}\s*=\s*process\.env\b")
_BINDING = re.compile(rf"^\s*({_NAME})")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _python_patterns(text):
    """The read patterns of a Python file; a bare `environ` is the environment only when the file imports it from `os`."""
    prefix = r"(?<![\w.])(?:os\.)?" if _PYTHON_IMPORT.search(text) else r"(?<![\w.])os\."
    return (re.compile(rf"(?<!del ){prefix}environ\s*\[\s*['\"]({_NAME})['\"]\s*\]{_NOT_WRITE}"),
            re.compile(rf"{prefix}environ\.get\s*\(\s*['\"]({_NAME})['\"]"),
            re.compile(rf"['\"]({_NAME})['\"]\s+in\s+{prefix}environ\b"), _GETENV)


def _commented(line, python):
    """Whether a line holds only a comment."""
    stripped = line.lstrip()
    return stripped.startswith("#") if python else stripped.startswith(("//", "/*", "*"))


def _reads(path, text):
    """The (name, line) of every literal environment read of a code file, in file order."""
    python = path.lower().endswith(doc_check.PYTHON)
    patterns = _python_patterns(text) if python else (*_NODE, _GETENV)
    matches = [(match.start(), match[1]) for pattern in patterns for match in pattern.finditer(text)]
    if not python:
        for block in _DESTRUCTURE.finditer(text):
            for part in block[1].split(","):
                binding = _BINDING.match(part)
                if binding:
                    matches.append((block.start(), binding[1]))
    lines = text.splitlines()
    found = []
    for offset, name in sorted(matches):
        number = text.count("\n", 0, offset) + 1
        if not _commented(lines[number - 1], python):
            found.append((name, number))
    return found


def _documented(root):
    """The words of the README and `docs` files of the project, and how many files were read."""
    words, checked = set(), 0
    for rel, text in project_scan.tracked_texts(root, rename_preview.DOC_EXTENSIONS):
        if text is not None and rename_preview._is_doc(rel):
            checked += 1
            words.update(_WORD.findall(text))
    return words, checked


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The environment variables the code reads that no README or `docs` file names.

    Parameters: root (str) project folder, a git repository; view_id (str or None) index view; limit (int) variables returned.
    Returns: dict {"variables": [{"name", "path", "line", "reads", "only_in_tests"}] with the variables read by code first,
    then by name; `path` and `line` are the first read (the first code read when code reads it, else the first test read),
    `reads` counts the read sites, "counts": {"read", "documented", "undocumented", "only_tests", "docs_checked"},
    "note": str}.
    Raises: ValueError when the code graph is empty or git cannot list the folder.
    """
    model = code_health._model(root, view_id)
    sites = {}
    for path in model.files():
        kind = model.kind_of(path)
        if kind not in ("code", "test") or not doc_check.supported(path):
            continue
        text = model.read(path)
        for name, line in _reads(path, text) if text else ():
            sites.setdefault(name, []).append((kind == "test", path, line))
    words, checked = _documented(root)
    rows = []
    for name, found in sites.items():
        if name in words:
            continue
        found.sort()
        _test, path, line = found[0]
        rows.append({"name": name, "path": path, "line": line, "reads": len(found), "only_in_tests": found[0][0]})
    rows.sort(key=lambda row: (row["only_in_tests"], row["name"]))
    counts = {"read": len(sites), "documented": len(sites) - len(rows), "undocumented": len(rows),
              "only_tests": sum(row["only_in_tests"] for row in rows), "docs_checked": checked}
    return {"variables": rows[:limit], "counts": counts, "note": NOTE}
