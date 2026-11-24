"""The neighborhood of a function, its callers and the functions it calls, as Mermaid flowchart text: the same data
`code_impact.impact` answers with, drawn by the flowchart writer of `doc_extras`. Nothing is written."""
from itertools import zip_longest

import code_impact
import doc_extras

DEFAULT_LIMIT = 30


def _label(site):
    """How a node is named: the function and, when known, its file."""
    return f"{site['function']} ({site['path']})" if site.get("path") else site["function"]


def neighborhood(root, symbol, view_id=None, depth=3, limit=DEFAULT_LIMIT):
    """The callers and the calls of a function as a Mermaid flowchart.

    Parameters: root (str) project folder; symbol (str) as `code_impact.impact` takes it; view_id (str or None) index view;
    depth (int) test depth, passed to `impact` and not drawn; limit (int) most functions drawn around the definitions,
    callers and callees taking turns so neither side hides the other.
    Returns: dict {"symbol", "mermaid" (str, the flowchart text), "counts": {"callers", "calls", "nodes", "omitted"} where
    callers and calls are the totals the graph holds, nodes the boxes drawn and omitted the calls left out, "view",
    "note": str}.
    Raises: ValueError as `code_impact.impact` does.
    """
    found = code_impact.impact(root, symbol, view_id, depth, limit)
    centres = {f"{d['path']}::{d['name']}": f"{d['name']} ({d['path']})" for d in found["definitions"]}
    default_centre = next(iter(centres.values())) if len(centres) == 1 else None
    labels, edges = list(centres.values()), []
    drawn = 0
    for caller, callee in zip_longest(found["callers"], found["calls"]):
        for site, incoming in ((caller, True), (callee, False)):
            if site is None:
                continue
            label = _label(site)
            if label not in labels:
                if drawn >= limit:
                    continue
                labels.append(label)
                drawn += 1
            centre = centres.get(site.get("of"), default_centre)
            edge = (label, centre, f"line {site['line']}") if incoming else (centre, label, f"line {site['line']}")
            if edge not in edges:
                edges.append(edge)
    counts = found["counts"]
    omitted = counts["callers"] + counts["calls"] - len(edges)
    note = found["note"]
    if omitted:
        note += f" {omitted} caller(s) or call(s) are left out of the drawing by limit={limit}."
    if not edges:
        note += " The function has no caller or call in the graph."
    return {"symbol": found["symbol"], "mermaid": "\n".join(doc_extras.flowchart(labels, edges)),
            "counts": {"callers": counts["callers"], "calls": counts["calls"], "nodes": len(labels), "omitted": omitted},
            "view": found["view"], "note": note}
