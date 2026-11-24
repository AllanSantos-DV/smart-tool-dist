"""Who owns a function: its authors by the lines inside the function's own range that still stand at HEAD.

The range comes from the code graph, which describes the working tree, and `git blame -L` reads it in the committed file:
a file with uncommitted edits, or one git does not track, is refused instead of answered with a range that may not match
(the check `symbol_history` makes). Authors are names only. Nothing is written.
"""
import ownership
import symbol_history

DEFAULT_LIMIT = 30
NOTE = ("Authors by the lines of the function that still stand at HEAD (`git blame -w -L` on the line range the code graph "
        "gives for the function in the working tree, which equals HEAD because files with uncommitted edits are refused), "
        "whitespace-only changes ignored; a pinned or older view_id can hold ranges that no longer match. Authors are the "
        "names git reports: the same person under two names counts twice unless a .mailmap joins them, and an author who "
        "only reformatted or moved code holds those lines.")


def report(root, symbol, view_id=None, limit=DEFAULT_LIMIT):
    """The authors of a function by surviving lines.

    Parameters: root (str) project folder; symbol (str) function, Class.method, class name or path::name (every
    definition sharing a bare name is answered); view_id (str or None) index view; limit (int) authors listed per
    definition, 1 to 200.
    Returns: dict {"symbol", "definitions": [{"name", "kind", "path", "lines": "start-end", "total_lines": int (lines that
    still stand), "authors": [{"author": name, "lines": int, "share": whole percent of total_lines}] most lines first, at
    most limit, "authors_total": int, "top_author", "top_share"}], "view", "note": str}.
    Raises: ValueError when symbol is blank or matches no function, limit is out of range, the code graph is empty, a
    definition's file has uncommitted edits or is not committed, or git fails (no repository, no history for the file).
    """
    symbol = symbol_history.symbol_name(symbol)
    if type(limit) is not int or not 1 <= limit <= symbol_history.MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {symbol_history.MAX_LIMIT}, got: {limit!r}")
    data, found = symbol_history.definitions(root, symbol, view_id)
    listed = []
    for entry in found:
        rows = ownership._blame(root, entry["path"], (entry["start_line"], entry["end_line"]))
        held = sum(count for _name, count in rows)
        top_name, top_count = rows[0] if rows else (None, 0)
        listed.append({"name": entry["name"], "kind": entry.get("kind"), "path": entry["path"],
                       "lines": f"{entry['start_line']}-{entry['end_line']}", "total_lines": held,
                       "authors": ownership._shares(rows, "lines", limit), "authors_total": len(rows),
                       "top_author": top_name, "top_share": round(100 * top_count / held) if held else 0})
    return {"symbol": symbol, "definitions": listed, "view": (data.get("selected") or {}).get("label"), "note": NOTE}
