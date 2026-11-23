"""The `graph` answer for one function with what an edit needs besides the call graph, in one call: its callers, the tests that
reach it, the files that usually change with its file and the decision notes that name it. Each part is the answer of the
module that owns it (`code_impact`, `code_health.coupled_with`, `dossier`); nothing is written.
"""
import code_health
import code_impact
import dossier


def _decisions(root, definitions, limit):
    """Decision notes and commit messages naming each definition, best first and without repeats.

    Returns: tuple (hits, note): hits is a list of dossier hits cut to `limit`; note is "" when every search ran whole, else
    the reasons a search was cut.
    """
    hits, seen, reasons = [], set(), []
    for definition in definitions:
        answer = dossier.notes_about(root, definition["path"], definition["name"])
        reasons += [reason for reason in answer["reasons"] if reason not in reasons]
        for hit in answer["hits"]:
            if hit["id"] not in seen:
                seen.add(hit["id"])
                hits.append(hit)
    return hits[:limit], f"The notes search was cut: {'; '.join(reasons)}" if reasons else ""


def brief(root, symbol, view_id=None, depth=3, limit=30):
    """Callers, reaching tests, change coupling and decision notes of one function.

    Parameters: root (str) project folder; symbol (str) as `code_impact.impact` takes it; view_id (str or None) index view;
    depth (int) call hops searched for tests; limit (int) rows of each list.
    Returns: dict {"symbol", "definitions", "callers", "tests", "counts": {"callers", "tests"}, "test_depth", "coupling":
    {"files", "note"}, "decisions": {"hits", "note"}, "diagnostics", "view", "note"} and "left_out" when the graph was cut;
    a note is "" when its part was computed whole, otherwise the reason it was not.
    Raises: ValueError when the symbol is not found or the code graph is empty, as `graph` does.
    """
    found = code_impact.impact(root, symbol, view_id, depth, limit)
    paths = sorted({definition["path"] for definition in found["definitions"]})
    coupling = code_health.coupled_with(root, paths, view_id, limit)
    hits, decisions_note = _decisions(root, found["definitions"], limit)
    return {
        "symbol": found["symbol"],
        "definitions": found["definitions"],
        "callers": found["callers"],
        "tests": found["tests"],
        "counts": {"callers": found["counts"]["callers"], "tests": found["counts"]["tests"]},
        "test_depth": found["test_depth"],
        "coupling": {"files": coupling["files"], "note": coupling["note"]},
        "decisions": {"hits": hits, "note": decisions_note},
        "diagnostics": found["diagnostics"],
        "view": found["view"],
        "note": found["note"],
        **({"left_out": found["left_out"]} if "left_out" in found else {}),
    }
