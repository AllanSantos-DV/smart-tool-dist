"""Functions of the whole project ranked by how risky they are to change: how long they are, how many other functions
call them, how often their file changed and whether a test reaches them.

The candidates and the test reach are those of `untested`; the churn is the commits of the file (`code_health.hotspots`).
Each function keeps its four raw numbers beside the score, so the order can be checked by hand. Nothing is written.
"""
import code_graph
import code_health
import code_impact
import index_profile
import index_scope
import pr_impact
import untested

RANK_RULE = ("score = (size + 1) * (fan_in + 1) * (commits + 1) * (2 when no test reaches the function, 1 when one does), "
             "highest first, then path and line. size is end_line - start_line; fan_in is the number of distinct "
             "functions outside the test files that call it, itself left out; commits is the number of commits that "
             "touched its file (0 for every file when no git history can be read); tested is whether a test reaches it "
             "through static calls within test_depth hops. The +1 terms keep a zero in one number from hiding the others.")


def _fan_in(callers, by_id, profile, symbol):
    """The distinct functions outside the test files that call the symbol, itself left out."""
    sources = {call["source"] for call in callers.get(symbol["id"], ())} - {symbol["id"]}
    return sum(1 for source in sources if source in by_id and index_profile.kind(by_id[source]["path"], profile) != "test")


def report(root, view_id=None, limit=30):
    """The functions of the indexed project ranked by size, callers, churn and test reach.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) functions returned.
    Returns: dict {"functions": [{"name", "path", "line", "size", "fan_in", "commits", "tested", "score"}] highest score first,
    "counts": {"functions", "untested"}, "rank_rule": str, "test_depth": int, "history_note": str (why the churn could not
    be read, "" otherwise; every file then counts 0 commits), "graph_note": str (what the analysis limits cut, "" when
    nothing), "note": str}.
    Raises: ValueError when the code graph is empty.
    """
    history = code_health.hotspots(root, view_id, pr_impact.ALL_ROWS)
    churn = {row["path"]: row["commits"] for row in history["files"]}
    data = code_graph.build_current(root, view_id, shared=True)
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    by_id = {s["id"]: s for s in data["symbols"]}
    callers, _callees = code_impact.call_index(data["calls"])
    rows = []
    for symbol in (s for s in data["symbols"] if untested._candidate(s, profile)):
        tested = bool(code_impact.reaching_tests(by_id, callers, profile, {symbol["id"]}, pr_impact.TEST_DEPTH))
        size = symbol["end_line"] - symbol["start_line"]
        fan_in = _fan_in(callers, by_id, profile, symbol)
        commits = churn.get(symbol["path"], 0)
        rows.append({"name": symbol["name"], "path": symbol["path"], "line": symbol["start_line"], "size": size,
                     "fan_in": fan_in, "commits": commits, "tested": tested,
                     "score": (size + 1) * (fan_in + 1) * (commits + 1) * (1 if tested else 2)})
    rows.sort(key=lambda row: (-row["score"], row["path"], row["line"]))
    return {"functions": rows[:limit],
            "counts": {"functions": len(rows), "untested": sum(not row["tested"] for row in rows)},
            "rank_rule": RANK_RULE,
            "test_depth": pr_impact.TEST_DEPTH,
            "history_note": history["note"],
            "graph_note": code_graph.left_out_sentence(data) or "",
            "note": code_impact.NOTE}
