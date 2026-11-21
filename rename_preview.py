"""What renaming a function leaves behind, asked before the rename: where it is defined, who calls it (the calls the
graph resolved and the calls it left unresolved under that name), the tests that reach it, the files that import its
module, the anchors of the generated documents and the lines of the README and `docs` text that name it. Nothing is
written; the lists are the places an agent edits or checks after changing the name.
"""
import posixpath

import code_graph
import code_impact
import doc_scope
import dossier
import project_scan
import removed_callers

DEFAULT_LIMIT = 30
TEST_DEPTH = 3
DOC_EXTENSIONS = (".md", ".rst", ".txt", ".html")
MAX_LINE_CHARS = 160
NOTE = ("A rename also needs a text search of the old name for what static analysis cannot see: strings, dynamic access "
        "and names built at run time.")


def _is_doc(rel):
    """Whether a tracked file is a README or sits in a `docs` folder, outside the generated documents (their anchors are
    listed apart)."""
    if rel.startswith(doc_scope.DOCS_DIR + "/") or not rel.lower().endswith(DOC_EXTENSIONS):
        return False
    return posixpath.basename(rel).upper().startswith("README") or "docs" in rel.split("/")[:-1]


def _unresolved_calls(data, definitions, limit):
    """Calls the graph could not resolve whose expression can mean one of the definitions, as `removed_callers` reads them.

    Returns: tuple (list of {"path", "line", "expression"} up to `limit`, int total).
    """
    found = {}
    for definition in definitions:
        short = definition["name"].rpartition(".")[2]
        stem = posixpath.splitext(posixpath.basename(definition["path"]))[0]
        for call in data.get("unresolved") or []:
            if removed_callers._names(call["expression"], short, stem, "." in definition["name"]):
                found[(call["path"], call["line"], call["expression"])] = None
    rows = [{"path": path, "line": line, "expression": expression} for path, line, expression in sorted(found)]
    return rows[:limit], len(rows)


def _mentions(root, definitions, limit):
    """Lines of the tracked README and `docs` text that name a definition: `path::name`, `module.name` or `Owner.name`,
    the name in backticks, or a compound name on its own (`dossier.mention_test`, which keeps a single plain word out).

    Returns: tuple (list of {"path", "line", "text"} up to `limit`, int total).
    Raises: ValueError when git cannot list the folder.
    """
    tests = [dossier.mention_test(item["path"], item["name"]) for item in definitions]
    literals = [f"{item['path']}::{item['name']}" for item in definitions]
    quoted = [f"`{item['name'].rpartition('.')[2]}`" for item in definitions]
    quoted += [text[:-1] + "(" for text in quoted]
    found, total = [], 0
    for rel, text in project_scan.tracked_texts(root, DOC_EXTENSIONS):
        if text is None or not _is_doc(rel):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if any(test(line) for test in tests) or any(literal in line for literal in literals) \
                    or any(mark in line for mark in quoted):
                total += 1
                if len(found) < limit:
                    found.append({"path": rel, "line": number, "text": line.strip()[:MAX_LINE_CHARS]})
    return found, total


def preview(root, symbol, view_id=None, limit=DEFAULT_LIMIT):
    """Everything a rename of one function touches.

    Parameters: root (str) project folder; symbol (str) function, `Class.method`, class or `path::name`; view_id (str or
    None) index view; limit (int) rows in each list.
    Returns: dict {"symbol", "definitions": [{"name", "kind", "path", "lines"}], "callers": [{"function", "path", "line"}],
    "unresolved_calls": [{"path", "line", "expression"}], "tests", "imported_by": [path], "doc_anchors": [{"file", "line",
    "id", "level", "current"}], "doc_mentions": [{"path", "line", "text"}], "counts": {list: total}, "doc_scope": str
    ("" when docs/scope exists, otherwise why no anchor could be listed), "note": str}.
    Raises: ValueError when the symbol is not found (with the closest names), the folder is not a git repository or a page of
    docs/scope is in an older format; RuntimeError when the code graph cannot be built.
    """
    found = code_impact.impact(root, symbol, view_id, TEST_DEPTH, limit)
    data = code_graph.build_current(root, view_id, shared=True)
    unresolved, unresolved_total = _unresolved_calls(data, found["definitions"], limit)
    mentions, mentions_total = _mentions(root, found["definitions"], limit)
    anchored = doc_scope.anchors(root, symbol, view_id)
    anchors = anchored["anchors"]
    counts = {**found["counts"], "unresolved_calls": unresolved_total, "doc_anchors": len(anchors),
              "doc_mentions": mentions_total}
    counts.pop("calls")
    return {"symbol": found["symbol"], "definitions": found["definitions"], "callers": found["callers"],
            "unresolved_calls": unresolved, "tests": found["tests"], "imported_by": found["imported_by"],
            "doc_anchors": anchors[:limit], "doc_mentions": mentions, "counts": counts,
            "doc_scope": anchored.get("guidance", ""), "note": f"{found['note']} {NOTE}"}
