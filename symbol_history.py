"""The commits that changed a function's own lines, newest first, with date, author and subject.

The lines come from the code graph, which describes the working tree, and `git log -L` reads them in the committed file:
a file with uncommitted edits is refused instead of answered with ranges that may not match. Commits are listed whether
or not their message names the function; authors are names only. Nothing is written.
"""
import affected_tests
import code_graph
import code_impact

DEFAULT_LIMIT = 30
MAX_LIMIT = 200
_FIELDS = "%h%x1f%ad%x1f%an%x1f%s"
NOTE = ("Commits found by `git log -L` on the line range the code graph gives for the function in the working tree, "
        "which equals HEAD because files with uncommitted edits are refused; a pinned or older view_id can hold ranges "
        "that no longer match. Commits that only moved the function without changing its lines are not listed.")


def _clean(root, path):
    """Raises ValueError unless the file is committed and the working tree and index hold no change to it."""
    status = affected_tests._git(root, "-c", "core.quotepath=false", "status", "--porcelain", "-z", "--", path)
    if not status:
        return
    if status.startswith(("??", "A")):
        raise ValueError(f"{path} is not committed yet, so it has no history to list; commit it first.")
    raise ValueError(f"{path} has uncommitted edits, and the line ranges of the code graph describe the working tree, not "
                     "the commits; commit or stash them and ask again. Another definition of the name can be asked on its "
                     "own with path::name.")


def _commits(root, symbol):
    """The (hash, date, author, subject) of every commit that changed the lines of `symbol`, newest first."""
    out = affected_tests._git(root, "-c", "core.quotepath=false", "log", "-s", "--no-color", "--date=short",
                              f"--format={_FIELDS}", f"-L{symbol['start_line']},{symbol['end_line']}:{symbol['path']}")
    rows = []
    for line in out.splitlines():
        fields = line.split("\x1f")
        if len(fields) == 4:
            rows.append(fields)
    return rows


def symbol_name(symbol):
    """The trimmed `symbol`; raises ValueError when it is not text or is blank."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Pass symbol: a function, method (Class.method) or class name, or path::name.")
    return symbol.strip()


def definitions(root, symbol, view_id=None):
    """The definitions of `symbol` in the code graph, every file of them committed and unedited.

    Returns: (graph data, list of matching symbols). Raises: ValueError when the code graph is empty, `symbol` matches no
    function, or a definition's file has uncommitted edits, is not committed or has no git history.
    """
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    found = code_impact.resolve(data, symbol)
    for path in sorted({s["path"] for s in found}):
        _clean(root, path)
    return data, found


def report(root, symbol, view_id=None, limit=DEFAULT_LIMIT):
    """The history of a function's own lines.

    Parameters: root (str) project folder; symbol (str) function, Class.method, class name or path::name (every
    definition sharing a bare name is answered); view_id (str or None) index view; limit (int) commits listed per
    definition, 1 to 200.
    Returns: dict {"symbol", "definitions": [{"name", "kind", "path", "lines": "start-end", "total": int, "commits":
    [{"commit": short hash, "date": YYYY-MM-DD, "author": name, "subject"}] newest first, at most limit}], "view",
    "note": str}.
    Raises: ValueError when symbol is blank or matches no function, limit is out of range, the code graph is empty, a
    definition's file has uncommitted edits or is not committed, or git fails (no repository, no history for the file).
    """
    symbol = symbol_name(symbol)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}, got: {limit!r}")
    data, found = definitions(root, symbol, view_id)
    listed = []
    for entry in found:
        commits = _commits(root, entry)
        listed.append({"name": entry["name"], "kind": entry.get("kind"), "path": entry["path"],
                       "lines": f"{entry['start_line']}-{entry['end_line']}", "total": len(commits),
                       "commits": [{"commit": h, "date": date, "author": author, "subject": subject}
                                   for h, date, author, subject in commits[:limit]]})
    return {"symbol": symbol, "definitions": listed, "view": (data.get("selected") or {}).get("label"),
            "note": NOTE}
