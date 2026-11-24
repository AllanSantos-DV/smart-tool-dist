"""What changing a function touches, from the indexed code graph: where it is defined, who calls it, what it calls,
which tests reach it through static calls and which files import its module, each with the first line of its
docstring. Answers the question an agent asks before an edit without a chain of searches and file reads."""
import difflib
import os
import re
from collections import defaultdict

import code_graph
import doc_check
import index_profile
import index_scope

MAX_DEPTH = 4
NOTE = ("Static analysis of the indexed snapshot plus the files changed in the working tree: calls made through callbacks, dynamic dispatch or names built at "
        "runtime are not seen, so treat an empty list as 'none found', not as 'none exist'.")


def _matches(symbols, name):
    path, _sep, wanted = name.strip().rpartition("::")
    path = path.replace("\\", "/").removeprefix("./")
    pool = [s for s in symbols if s["path"] == path or s["path"].endswith("/" + path)] if path else symbols
    exact = [s for s in pool if s["id"] == wanted or s["name"] == wanted]
    return exact or [s for s in pool if s["name"].split(".")[-1] == wanted]


def _closest_names(symbols, name, limit=3):
    """Up to `limit` indexed function or class names that resemble `name` (case ignored), best first."""
    wanted = name.strip().rpartition("::")[2].casefold()
    by_lower = {}
    for symbol in symbols:
        by_lower.setdefault(symbol["name"].split(".")[-1].casefold(), symbol["name"].split(".")[-1])
    return [by_lower[key] for key in difflib.get_close_matches(wanted, list(by_lower), n=limit, cutoff=0.6)]


def resolve(data, name, argument=None):
    """The symbols of a graph that `name` designates.

    Raises: ValueError when none does, with the closest indexed names and the analysis problems; `argument` names the
    parameter that held `name` when the caller takes more than one.
    """
    symbols = data.get("symbols") or []
    found = _matches(symbols, name)
    if found:
        return found
    diagnostics = [d.get("reason") for d in data.get("diagnostics") or [] if d.get("reason")]
    cause = f" Analysis problems: {'; '.join(diagnostics)}" if diagnostics else ""
    close = _closest_names(symbols, name)
    hint = f" Closest names: {', '.join(close)}." if close else ""
    given = f" (the {argument})" if argument else ""
    raise ValueError(f"No function or class named {name!r}{given} in the indexed view; check the name or reindex.{hint}{cause}")


def _site(by_id, symbol_id, line, of=None):
    symbol = by_id.get(symbol_id) or {}
    site = {"id": symbol_id, "function": symbol.get("name", symbol_id), "path": symbol.get("path"), "line": line}
    if of:
        site["of"] = of
    return site


def summaries(root, symbols, wanted_ids):
    """First docstring line of the wanted symbols, read from the working tree once per file."""
    by_path = defaultdict(list)
    for symbol in symbols:
        by_path[symbol["path"]].append(symbol)
    docs = {}
    for path in {s["path"] for s in symbols if s["id"] in wanted_ids}:
        try:
            with open(os.path.join(root, path), encoding="utf-8") as stream:
                source = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        for function in doc_check.functions(path, source, by_path[path]):
            if function["doc"]:
                docs[(path, function["start"])] = function["doc"]
    return {s["id"]: docs[(s["path"], s["start_line"])] for s in symbols
            if s["id"] in wanted_ids and (s["path"], s["start_line"]) in docs}


_TEST_TITLE = re.compile(r"""\b(test|it|describe|suite)(?:\.\w+)?\s*\(\s*(['"`])(.+?)\2""")


def _name_test_callbacks(root, by_id, sites):
    """Anonymous callbacks registered with test('title', fn), it(), describe() are named after their title, so a test
    reads 'test "loads the config"' instead of 'callback'."""
    lines_of = {}
    for site in sites:
        symbol = by_id.get(site.get("id")) or {}
        if symbol.get("name", "").split(".")[-1] not in ("callback", "anonymous"):
            continue
        path = symbol["path"]
        if path not in lines_of:
            try:
                with open(os.path.join(root, path), encoding="utf-8") as stream:
                    lines_of[path] = stream.read().splitlines()
            except (OSError, UnicodeDecodeError):
                lines_of[path] = []
        start = symbol["start_line"]
        text = " ".join(lines_of[path][max(start - 2, 0):start])
        match = _TEST_TITLE.search(text)
        if match:
            site["function"] = f'{match.group(1)} "{match.group(3)[:80]}"'


def call_index(calls):
    """The calls of a graph grouped by target and by source: (callers, callees), each a defaultdict of id -> list of call."""
    callers, callees = defaultdict(list), defaultdict(list)
    for call in calls:
        callers[call["target"]].append(call)
        callees[call["source"]].append(call)
    return callers, callees


def reaching_tests(by_id, callers, profile, targets, depth, label=None):
    """The test functions that reach `targets` through static calls, searched breadth first from each target.

    Parameters: by_id (dict) symbols by id; callers (dict) the `call_index` callers; profile (dict) the index profile that
    tells a test file from a code file; targets (set of str) symbol ids; depth (int) call hops searched; label (dict or
    None) symbol id -> text put in the `of` field of its tests.
    Returns: dict test symbol id -> site ({id, function, path, line, hops, and of when labelled}), the nearest hop kept.
    """
    label = label or {}
    tests = {}
    for origin in targets:
        seen, frontier = set(targets), {origin}
        for hops in range(1, depth + 1):
            reached = set()
            for target in frontier:
                for call in callers[target]:
                    source = call["source"]
                    if source in seen:
                        continue
                    seen.add(source)
                    reached.add(source)
                    caller = by_id.get(source)
                    if caller and index_profile.kind(caller["path"], profile) == "test" and source not in tests:
                        tests[source] = {**_site(by_id, source, caller["start_line"], label.get(origin)), "hops": hops}
            frontier = reached
    return tests


def impact(root, symbol, view_id=None, depth=3, limit=30):
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Pass symbol: a function, method (Class.method) or class name.")
    if type(depth) is not int or not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be an integer from 1 to {MAX_DEPTH}.")
    data = code_graph.build_current(root, view_id, shared=True)
    found = resolve(data, symbol)
    diagnostics = [d.get("reason") for d in data.get("diagnostics") or [] if d.get("reason")]
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    by_id = {s["id"]: s for s in data["symbols"]}
    callers, callees = call_index(data["calls"])
    targets = {s["id"] for s in found}
    label = {s["id"]: f"{s['path']}::{s['name']}" for s in found} if len(found) > 1 else {}
    tests = reaching_tests(by_id, callers, profile, targets, depth, label)
    direct = [_site(by_id, c["source"], c["line"], label.get(t)) for t in targets for c in callers[t]
              if c["source"] not in targets]
    outgoing = [_site(by_id, c["target"], c["line"], label.get(t)) for t in targets for c in callees[t]
                if c["target"] not in targets]
    files = {s["path"] for s in found}
    importers = sorted({d["source"] for d in data.get("dependencies") or [] if d.get("target") in files and d["source"] not in files})
    ordered_tests = sorted(tests.values(), key=lambda t: (t["hops"], t["path"], t["line"]))
    shown = direct[:limit] + outgoing[:limit] + ordered_tests[:limit]
    _name_test_callbacks(root, by_id, shown)
    docs = summaries(root, data["symbols"], targets | {site["id"] for site in shown})
    for site in shown:
        doc = docs.get(site.pop("id"))
        if doc:
            site["doc"] = doc
    definitions = []
    for s in found:
        entry = {"name": s["name"], "kind": s.get("kind"), "path": s["path"], "lines": f"{s['start_line']}-{s['end_line']}"}
        if docs.get(s["id"]):
            entry["doc"] = docs[s["id"]]
        definitions.append(entry)
    return {
        "symbol": symbol.strip(),
        "definitions": definitions,
        "callers": direct[:limit],
        "calls": outgoing[:limit],
        "tests": ordered_tests[:limit],
        "imported_by": importers[:limit],
        "counts": {"callers": len(direct), "calls": len(outgoing), "tests": len(ordered_tests),
                   "imported_by": len(importers)},
        "test_depth": depth,
        "diagnostics": diagnostics,
        "view": (data.get("selected") or {}).get("label"),
        "note": NOTE + (f" {len(found)} definitions share this name; each entry says which one ('of'); pass "
                        "path::name to keep one." if label else "")
                + (f" {code_graph.left_out_sentence(data)} Callers and tests beyond the cut are not listed." if data["truncated"] else ""),
        **({"left_out": data["left_out"]} if data["truncated"] else {}),
    }


def for_agent(data, limit):
    """The file-focused graph without what an agent never reads (view list, display limits) and with lists capped."""
    for key in ("symbols", "dependencies", "calls", "unresolved", "diagnostics"):
        items = data.get(key) or []
        if len(items) > limit:
            data[key] = items[:limit]
            data.setdefault("omitted", {})[key] = len(items) - limit
    for key in ("views", "limits", "languages", "current_view", "generated_at", "cache_hit", "read_only", "source"):
        data.pop(key, None)
    return data
