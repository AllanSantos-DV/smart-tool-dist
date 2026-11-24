"""The shortest static call chain from one function to another, with the line of each call site.

Breadth first over the call edges `code_impact` reads (the indexed snapshot plus the files changed in the working tree).
An answer without a chain says whether the search ran out of calls to follow or was cut by the hop cap; calls made
through dynamic dispatch, callbacks or names built at runtime are not edges, so it never claims that no path exists.
Nothing is written.
"""
import code_graph
import code_impact

DEFAULT_MAX_HOPS = 6
MAX_HOPS = 10

_INVISIBLE = "Calls made through dynamic dispatch, callbacks or names built at runtime are not seen"


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pass symbol and target: each a function, method (Class.method) or class name, or path::name.")
    return value.strip()


def _hops(value):
    if value is None:
        return DEFAULT_MAX_HOPS
    if type(value) is not int or not 1 <= value <= MAX_HOPS:
        raise ValueError(f"max_hops must be an integer from 1 to {MAX_HOPS}, got: {value!r}")
    return value


def _search(callees, starts, goals, max_hops):
    """Breadth-first search from `starts` to the nearest of `goals`.

    Returns: (goal id or None, parent map id -> (previous id, call) or None, hops walked, exhausted) where exhausted is
    True when the search stopped because no unvisited function was left to follow.
    """
    parent = {start: None for start in starts}
    if starts & goals:
        return min(starts & goals), parent, 0, True
    frontier = sorted(starts)
    for hops in range(1, max_hops + 1):
        following = []
        for source in frontier:
            for call in callees[source]:
                target = call["target"]
                if target in parent:
                    continue
                parent[target] = (source, call)
                if target in goals:
                    return target, parent, hops, False
                following.append(target)
        if not following:
            return None, parent, hops - 1, True
        frontier = following
    return None, parent, max_hops, False


def _chain(by_id, parent, goal):
    """The call sites from the start to `goal`, in call order."""
    steps = []
    while parent[goal]:
        source, call = parent[goal]
        site = code_impact._site(by_id, source, call["line"])
        site.pop("id")
        site["calls"] = (by_id.get(goal) or {}).get("name", goal)
        steps.append(site)
        goal = source
    return steps[::-1]


def _note(symbol, target, found, hops, exhausted, max_hops, data):
    if found:
        text = ("Shortest chain over the static call edges; each step is a call site: `line` is where `function` calls "
                "`calls`. Other chains of the same or greater length may exist.")
    elif exhausted:
        text = (f"No static path: every function reachable from {symbol!r} through static calls was followed "
                f"(exhausted after {hops} hops) and none is {target!r}. {_INVISIBLE}, so this does not prove that no "
                "path exists.")
    else:
        text = (f"No static path found: the search was cut at {max_hops} hops, raise max_hops to look further. "
                f"{_INVISIBLE}.")
    if data.get("truncated"):
        text += f" {code_graph.left_out_sentence(data)} Calls beyond the cut are not followed."
    return text


def report(root, symbol, target, view_id=None, max_hops=None):
    """The shortest static call chain from `symbol` to `target`.

    Parameters: root (str) project folder; symbol (str) where the chain starts; target (str) where it ends, each a
    function, Class.method, class name or path::name (every definition sharing a bare name is searched); view_id (str or
    None) index view; max_hops (int or None) longest chain searched, 1 to 10, 6 when None.
    Returns: dict {"symbol", "target", "found": bool, "hops": int or None, "chain": [{"function", "path", "line",
    "calls"}] one entry per call site in call order (empty when not found or when symbol is target), "max_hops",
    "matched": {"symbol": [path::name], "target": [path::name]}, "view", "note": str, "left_out" (only when the graph
    was cut)}.
    Raises: ValueError when a name is blank or matches no function, max_hops is out of range or the code graph is empty.
    """
    symbol, target = _name(symbol), _name(target)
    max_hops = _hops(max_hops)
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    starts = code_impact.resolve(data, symbol, "symbol")
    goals = code_impact.resolve(data, target, "target")
    by_id = {s["id"]: s for s in data["symbols"]}
    _callers, callees = code_impact.call_index(data["calls"])
    reached, parent, hops, exhausted = _search(callees, {s["id"] for s in starts}, {s["id"] for s in goals}, max_hops)
    found = reached is not None
    return {
        "symbol": symbol, "target": target, "found": found, "hops": hops if found else None,
        "chain": _chain(by_id, parent, reached) if found else [], "max_hops": max_hops,
        "matched": {"symbol": [f"{s['path']}::{s['name']}" for s in starts],
                    "target": [f"{s['path']}::{s['name']}" for s in goals]},
        "view": (data.get("selected") or {}).get("label"),
        "note": _note(symbol, target, found, hops, exhausted, max_hops, data),
        **({"left_out": data["left_out"]} if data.get("truncated") else {}),
    }
