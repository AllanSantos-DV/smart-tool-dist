"""Functions of the whole project that no test reaches, the most changed files first: the project-wide form of the
"touched functions no test reaches" list of `pr_impact`, ordered by the commits each file went through (`code_health.hotspots`).

A test reaches a function the way `code_impact` decides it, through static calls up to `pr_impact.TEST_DEPTH` hops. Nothing
is written.
"""
import code_graph
import code_health
import code_impact
import index_profile
import index_scope
import pr_impact

SKIPPED_NAMES = ("callback", "anonymous")
FUNCTION_KINDS = ("function", "method")


def _candidate(symbol, profile):
    """True for a named function or method of a code file; dunder methods and unnamed callbacks run from outside the graph."""
    name = symbol["name"].split(".")[-1]
    return (symbol.get("kind") in FUNCTION_KINDS and index_profile.kind(symbol["path"], profile) == "code"
            and name not in SKIPPED_NAMES and not (name.startswith("__") and name.endswith("__")))


def untested(root, view_id=None, limit=30):
    """The functions of the indexed project that no test reaches through static calls.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) functions returned.
    Returns: dict {"functions": [{"name", "path", "line", "commits"}] most changed file first, then path and line, "counts":
    {"untested", "functions"}, "test_depth": int, "history_note": str (why the churn could not be read, "" otherwise; every
    file then counts 0 commits), "graph_note": str (what the analysis limits cut, "" when nothing), "note": str}.
    Raises: ValueError when the code graph is empty.
    """
    history = code_health.hotspots(root, view_id, pr_impact.ALL_ROWS)
    churn = {row["path"]: row["commits"] for row in history["files"]}
    data = code_graph.build_current(root, view_id, shared=True)
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    by_id = {s["id"]: s for s in data["symbols"]}
    callers, _callees = code_impact.call_index(data["calls"])
    functions = [s for s in data["symbols"] if _candidate(s, profile)]
    missing = [s for s in functions
               if not code_impact.reaching_tests(by_id, callers, profile, {s["id"]}, pr_impact.TEST_DEPTH)]
    missing.sort(key=lambda s: (-churn.get(s["path"], 0), s["path"], s["start_line"]))
    return {"functions": [{"name": s["name"], "path": s["path"], "line": s["start_line"], "commits": churn.get(s["path"], 0)}
                          for s in missing[:limit]],
            "counts": {"untested": len(missing), "functions": len(functions)},
            "test_depth": pr_impact.TEST_DEPTH,
            "history_note": history["note"],
            "graph_note": code_graph.left_out_sentence(data) or "",
            "note": code_impact.NOTE}
