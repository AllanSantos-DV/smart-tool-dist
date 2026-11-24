"""How each unsafe call of `risky_calls` is reached: the shortest static call chain from an entry point of `entry_points` to
the function that holds it, with the line of each call.

The search is `call_path`'s, started from all the entry points at once over the call edges of the code graph. A finding with
no chain says whether the search ran out of calls to follow or was cut by the hop cap, and that a missing path is not proof
that the call is safe: calls made through dynamic dispatch, callbacks or names built at runtime are not edges. Nothing is
written.
"""
import call_path
import code_graph
import code_impact
import diff_sections
import entry_points
import risky_calls
import stack_trace

DEFAULT_LIMIT = risky_calls.DEFAULT_LIMIT
NOTE = ("Each finding of `risky_calls` is placed in the innermost function or class of the code graph that holds its line, "
        "and the shortest chain of static calls from an entry point (`entry_points`) to it is searched; each step is a call "
        "site: `line` is where `function` calls `calls`. Other chains of the same or greater length may exist. A finding "
        "with found=false has no static path within max_hops, and that is not proof that the call is safe: "
        f"{call_path._INVISIBLE}, and a call made at module level belongs to no function.")


def _why(label, hops, exhausted, max_hops):
    """Why no chain was found to the function `label`."""
    if exhausted:
        return (f"no static call chain from an entry point reaches it: every function reachable from the entry points "
                f"through static calls was followed (exhausted after {diff_sections._hops(hops)}) and none is {label}. "
                f"{call_path._INVISIBLE}, so a missing path is not proof that the call is safe.")
    return (f"no static call chain from an entry point was found within {diff_sections._hops(max_hops)}: the search was cut, "
            f"raise max_hops to look further. {call_path._INVISIBLE}; a missing path is not proof that the call is safe.")


def report(root, view_id=None, limit=DEFAULT_LIMIT, max_hops=None):
    """The chain from an entry point to the function of each unsafe call.

    Parameters: root (str) project folder, a git repository; view_id (str or None) index view; limit (int) findings
    searched (`risky_calls.scan`'s limit); max_hops (int or None) longest chain searched, 1 to 10, 6 when None.
    Returns: dict {"findings": [{"path", "line", "kind" (as `risky_calls` gives them), "function": "path::name" of the
    function that holds the call, None when none does, "found": bool, "hops": int or None, "chain": [{"function", "path",
    "line", "calls"}] one entry per call site from the entry point (empty when the function is one or none was found),
    "why": str (why there is no chain, "" when there is one)}] in file and line order, "total": int (findings of the
    project before limit), "reached": int (findings with a chain), "max_hops", "unparsed": [{"path", "why"}], "view",
    "note": str, "left_out" (only when the graph was cut)}.
    Raises: ValueError when max_hops is out of range, the code graph is empty, or git cannot list the folder.
    """
    max_hops = call_path._hops(max_hops)
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    scan = risky_calls.scan(root, limit)
    starts = diff_sections._entry_ids(data, entry_points.report(root, view_id, limit=diff_sections.ALL_ROWS))
    by_id = {symbol["id"]: symbol for symbol in data["symbols"]}
    by_path = {}
    for symbol in data["symbols"]:
        by_path.setdefault(symbol["path"], []).append(symbol)
    _callers, callees = code_impact.call_index(data["calls"])

    reached = {}
    rows = []
    for finding in scan["findings"]:
        holder = stack_trace._enclosing(by_path, [finding["path"]], finding["line"])
        row = {**finding, "function": None, "found": False, "hops": None, "chain": [], "why": ""}
        rows.append(row)
        if holder is None:
            row["why"] = ("the call is not inside a function or class of the code graph (module-level code, or a file it "
                          "does not index), so there is no function to search a chain to; that is not proof that it is "
                          "safe.")
            continue
        row["function"] = label = f"{holder['path']}::{holder['name']}"
        if not starts:
            row["why"] = "no entry point of the project was found in the code graph, so no chain can start."
            continue
        if holder["id"] not in reached:
            reached[holder["id"]] = call_path._search(callees, starts, {holder["id"]}, max_hops)
        goal, parent, hops, exhausted = reached[holder["id"]]
        if goal is None:
            row["why"] = _why(label, hops, exhausted, max_hops)
        else:
            row.update(found=True, hops=hops, chain=call_path._chain(by_id, parent, goal))
    more = scan["total"] - len(rows)
    note = NOTE + (f" {more} more finding(s) beyond limit were not searched." if more > 0 else "")
    if data.get("truncated"):
        note += f" {code_graph.left_out_sentence(data)} Calls beyond the cut are not followed."
    return {"findings": rows, "total": scan["total"], "reached": sum(row["found"] for row in rows), "max_hops": max_hops,
            "unparsed": scan["unparsed"], "view": (data.get("selected") or {}).get("label"), "note": note,
            **({"left_out": data["left_out"]} if data.get("truncated") else {})}
