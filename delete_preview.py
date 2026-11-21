"""What deleting a file or one function or class breaks, asked before the deletion: for a file, the modules that import it,
the Markdown and HTML links and the README and `docs` lines that point at it, the anchors of the generated documents and the
tests that reach it (the readers `move_preview` uses); for a symbol, the call sites that would be left naming it, the
imports of its name and the tests that reach it (the readers `removed_callers` and `code_impact` use). Nothing is deleted or
written; the lists are the places an agent edits after the deletion.
"""
import os

import code_graph
import code_impact
import doc_check
import move_preview
import removed_callers

DEFAULT_LIMIT = 30
TEST_DEPTH = 3
SUMMARY_SITES = 5


def _target(root, path):
    """The project-relative path of an existing file.

    Raises: ValueError when `path` is not a string, is absolute or leaves the project, does not exist, resolves outside the
    project or is a folder.
    """
    path = move_preview._relative(path, "path")
    full = os.path.join(root, *path.split("/"))
    if not os.path.lexists(full):
        raise ValueError(f"path {path!r} does not exist in the project; nothing to delete.")
    if not move_preview._inside(root, path):
        raise ValueError(f"path {path!r} resolves outside the project folder.")
    if os.path.isdir(full):
        raise ValueError(f"path {path!r} is a folder; pass a file, or a file with a symbol.")
    return path


def _symbol_name(symbol):
    """The bare function, `Class.method` or class name asked for.

    Raises: ValueError when it is blank, not a string or already carries a file (`path::name`): the file is `path`.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be the name of a function, Class.method or class in path.")
    if "::" in symbol:
        raise ValueError(f"symbol {symbol!r} names a file: pass the file as path and only the name as symbol.")
    return symbol.strip()


def _blanked(text, definitions):
    """`text` with the line ranges ("12-30") of the definitions emptied, as the file would read after deleting them; the
    lines stay in place so the line numbers of what remains are those of the file on disk."""
    cut = set()
    for item in definitions:
        first, _, last = item["lines"].partition("-")
        cut.update(range(int(first), int(last) + 1))
    return "".join("\n" if number in cut else line for number, line in enumerate(text.splitlines(keepends=True), 1))


def _read(root, path):
    try:
        with open(os.path.join(root, *path.split("/")), encoding="utf-8") as stream:
            return stream.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f"path {path!r} is not UTF-8 text, so the deletion of a symbol in it cannot be previewed: "
                         f"{exc}") from exc


def _file(root, path, view_id, limit):
    """The preview for deleting a whole file."""
    links, mentions = move_preview._scan(root, path)
    importers, tests, totals, graph_note = move_preview._graph(root, path, view_id, limit)
    anchors, guidance = move_preview._doc_anchors(root, path)
    tests = [{**row, "why": row["why"].replace("moved file", "deleted file")} for row in tests]
    counts = {"imported_by": len(importers), "links": len(links), "doc_mentions": len(mentions),
              "doc_anchors": len(anchors), "tests": len(tests), **totals}
    summary = (f"Deleting {path} leaves {counts['imported_by']} import(s), {counts['links']} link(s), "
               f"{counts['doc_mentions']} doc line(s), {counts['doc_anchors']} docs/scope anchor(s) and {counts['tests']} "
               "test(s) pointing at it.")
    return {"path": path, "symbol": None, "kind": "file", "imported_by": importers[:limit], "links": links[:limit],
            "doc_mentions": mentions[:limit], "doc_anchors": anchors[:limit], "tests": tests[:limit], "counts": counts,
            "doc_scope": guidance, "summary": summary, "note": f"{graph_note} {move_preview.NOTE}".strip()}


def _symbol(root, path, symbol, view_id, limit):
    """The preview for deleting one function or class of a file.

    Every definition of the name in the file goes: the call sites are those of `removed_callers._sites` for the name and for
    every function the cut takes with it (the methods of a deleted class), read against the file as it would be left.
    """
    found = code_impact.impact(root, f"{path}::{symbol}", view_id, TEST_DEPTH, limit)
    definitions = [item for item in found["definitions"] if item["path"] == path]
    if not definitions:
        raise ValueError(f"No function or class named {symbol!r} in {path}; it is defined in "
                         f"{', '.join(sorted({item['path'] for item in found['definitions']}))}.")
    before = _read(root, path)
    after = _blanked(before, definitions)
    names = {item["name"].rpartition(".")[2]: item["name"] for item in definitions}
    if doc_check.supported(path):
        for short, name in removed_callers._gone(path, before, after).items():
            names.setdefault(short, name)
    data = code_graph.build_current(root, view_id, shared=True)
    rows = sorted(({"function": name, "path": site_path, "line": line}
                   for short, name in names.items()
                   for site_path, line in removed_callers._sites(root, data, path, name, short, after)),
                  key=lambda row: (row["path"], row["line"], row["function"]))
    counts = {"callers": len(rows), "tests": found["counts"]["tests"]}
    sites = removed_callers.sites_text({"callers": rows[:SUMMARY_SITES], "total": len(rows)})
    summary = (f"Deleting {path}::{symbol} leaves {counts['callers']} site(s) naming it" + (f" ({sites})" if rows else "")
               + f"; {counts['tests']} test(s) reach it.")
    note = " ".join(part for part in (found["note"], removed_callers._note(data)) if part)
    return {"path": path, "symbol": symbol, "kind": "symbol", "definitions": definitions,
            "removed": sorted(names.values()), "callers": rows[:limit], "tests": found["tests"], "counts": counts,
            "summary": summary, "note": note}


def preview(root, path, symbol=None, view_id=None, limit=DEFAULT_LIMIT):
    """Everything deleting `path`, or only `symbol` in it, leaves pointing at what is gone.

    Parameters: root (str) project folder; path (str) file relative to it; symbol (str or None) function, `Class.method` or
    class defined in `path`; view_id (str or None) index view; limit (int) rows in each list.
    Returns: dict. For a file, {"path", "symbol": None, "kind": "file", "imported_by": [{"path", "line", "import"}], "links":
    [{"path", "line", "link", "anchor", "root_relative"}], "doc_mentions": [{"path", "line", "text"}], "doc_anchors":
    [{"file", "line", "id", "level"}], "tests": [{"path", "why"}], "counts", "doc_scope", "summary", "note"}. For a symbol,
    {"path", "symbol", "kind": "symbol", "definitions": [{"name", "kind", "path", "lines"}], "removed": [qualified names that
    go with the cut], "callers": [{"function", "path", "line"}], "tests": [{"function", "path", "line", "hops"}], "counts":
    {"callers", "tests"}, "summary", "note"}; the callers are those of the current view.
    Raises: ValueError when `path` is missing, outside the project or a folder, `symbol` is blank, names a file or is not
    defined in `path` (with the closest names), the folder is not a git repository or a page of docs/scope is in an older
    format; RuntimeError when the code graph cannot be built.
    """
    path = _target(root, path)
    if symbol is None:
        return _file(root, path, view_id, limit)
    return _symbol(root, path, _symbol_name(symbol), view_id, limit)
