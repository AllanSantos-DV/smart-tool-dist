"""Three sections of a change review that read the code of a diff against its base: the unsafe calls it adds, the touched
functions whose complexity grew and, for each touched function, one static call chain from an entry point of the project.
Each is advisory: nothing here blocks, and a check that cannot run says why instead of coming back empty.
"""
import call_path
import code_graph
import code_impact
import complexity
import entry_points
import risky_calls

ALL_ROWS = 100000


def _section(findings=(), problem="", note=""):
    return {"findings": list(findings), "problem": problem, "note": note}


def risky(versions):
    """The unsafe calls the changed files add: `risky_calls.added` over the text at the base and now.

    Parameters: versions (iterable) of (project-relative path, absolute path, text before or None for a new file, text after
    or None for a deleted one), as `pr_impact._versions` gives them.
    Returns: dict {"findings": [{"path", "line", "kind"}] in file and line order, "problem": str (the files that could not be
    read, "" when all were), "note": ""}. The source text of a call is never returned.
    """
    findings, problems = [], []
    for rel, _full, before, after in versions:
        if after is None:
            continue
        try:
            findings += [{"path": rel, "line": item["line"], "kind": item["kind"]} for item in risky_calls.added(rel, before, after)]
        except ValueError as exc:
            problems.append(str(exc))
    return _section(findings, "; ".join(problems))


def describe_risky(item):
    """One sentence for a finding of `risky`."""
    return f"{item['path']}:{item['line']} adds `{item['kind']}`"


def grown(versions, touched):
    """The touched functions whose complexity is higher now than at the base, from `complexity.growth`.

    Parameters: versions (iterable) as for `risky`; touched (list of str) the `path::name` labels of the touched functions; a
    function the base did not define, or whose label names a callback, has no number to compare and is left out.
    Returns: dict {"findings": [{"path", "name", "before", "after"}] in file order, "problem": str (the files whose
    complexity could not be counted, "" when all were), "note": ""}.
    """
    names = {}
    for label in touched:
        rel, separator, name = label.partition("::")
        if separator:
            names.setdefault(rel, []).append(name)
    findings, problems = [], []
    for rel, _full, before, after in versions:
        if rel in names and before is not None and after is not None:
            try:
                findings += [{"path": rel, **item} for item in complexity.growth(rel, before, after, names[rel])]
            except ValueError as exc:
                problems.append(str(exc))
    return _section(findings, "; ".join(problems))


def describe_grown(item):
    """One sentence for a finding of `grown`, with both numbers."""
    return f"`{item['path']}::{item['name']}`: complexity {item['before']} -> {item['after']} (+{item['after'] - item['before']})"


def _entry_ids(data, report):
    """The graph ids of the functions the entry-point report lists (a `__main__` block and a manifest file are not graph
    functions and have no name or line)."""
    named = {(row["path"], row["name"]) for group in report["layers"] for row in group["entries"]
             if row["name"] and row["line"]}
    return {symbol["id"] for symbol in data["symbols"] if (symbol["path"], symbol["name"]) in named}


def _hops(count):
    return f"{count} hop{'' if count == 1 else 's'}"


def reached(root, view_id, touched):
    """One shortest static call chain from an entry point of the project to each touched function.

    Breadth first from all the entry points at once over the call edges of the code graph (`call_path._search`), so the
    chain found is the nearest entry point's.

    Parameters: root (str) project folder; view_id (str or None) index view; touched (list of str) the `path::name` labels of
    the touched functions.
    Returns: dict {"findings": [{"function": label, "found": bool, "hops": int or None, "chain": [{"function", "path", "line",
    "calls"}] (the call sites from the entry point, empty when the function is one), "why": str (why there is no chain, ""
    when there is one)}] in the order of `touched`, "problem": str (why the graph or the entry points could not be read, ""
    when they were), "note": str (what the graph cut, "" when nothing)}.
    """
    if not touched:
        return _section()
    try:
        entries = entry_points.report(root, view_id, limit=ALL_ROWS)
        data = code_graph.build_current(root, view_id, shared=True)
    except (OSError, ValueError, RuntimeError) as exc:
        return _section(problem=f"the entry points could not be read: {exc}")
    starts = _entry_ids(data, entries)
    by_id = {symbol["id"]: symbol for symbol in data["symbols"]}
    _callers, callees = code_impact.call_index(data["calls"])
    rows = []
    for label in touched:
        row = {"function": label, "found": False, "hops": None, "chain": [], "why": ""}
        rows.append(row)
        try:
            goals = {symbol["id"] for symbol in code_impact.resolve(data, label)}
        except ValueError:
            row["why"] = "not found in the code graph under that name."
            continue
        if not starts:
            row["why"] = "no entry point of the project was found in the code graph."
            continue
        goal, parent, hops, exhausted = call_path._search(callees, starts, goals, call_path.DEFAULT_MAX_HOPS)
        if goal is None:
            row["why"] = ("no static call chain from an entry point reaches it. Calls made through dynamic dispatch, callbacks "
                          "or names built at run time are not seen." if exhausted else
                          f"no static call chain from an entry point was found within {_hops(call_path.DEFAULT_MAX_HOPS)}.")
            continue
        row.update(found=True, hops=hops, chain=call_path._chain(by_id, parent, goal))
    cut = f"{code_graph.left_out_sentence(data)} Calls beyond the cut are not followed." if data.get("truncated") else ""
    return _section(rows, note=cut)


def describe_reached(item):
    """One sentence for a row of `reached`: the chain from the entry point, or why there is none."""
    if not item["found"]:
        return f"`{item['function']}`: {item['why']}"
    if not item["chain"]:
        return f"`{item['function']}` is itself an entry point"
    steps = " -> ".join(f"`{step['function']}` ({step['path']}:{step['line']})" for step in item["chain"])
    return (f"`{item['function']}` is reached from the entry point `{item['chain'][0]['function']}`: {steps} -> "
            f"`{item['chain'][-1]['calls']}`")
