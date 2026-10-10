"""What changing a function touches, from the indexed code graph: where it is defined, who calls it, what it calls,
which tests reach it through static calls and which files import its module, each with the first line of its
docstring. Answers the question an agent asks before an edit without a chain of searches and file reads."""
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


def impact(root, symbol, view_id=None, depth=3, limit=30):
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Pass symbol: a function, method (Class.method) or class name.")
    if type(depth) is not int or not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be an integer from 1 to {MAX_DEPTH}.")
    data = code_graph.build_current(root, view_id)
    found = _matches(data.get("symbols") or [], symbol)
    diagnostics = [d.get("reason") for d in data.get("diagnostics") or [] if d.get("reason")]
    if not found:
        cause = f" Analysis problems: {'; '.join(diagnostics)}" if diagnostics else ""
        raise ValueError(f"No function or class named {symbol!r} in the indexed view; check the name or reindex.{cause}")
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    by_id = {s["id"]: s for s in data["symbols"]}
    callers, callees = defaultdict(list), defaultdict(list)
    for call in data["calls"]:
        callers[call["target"]].append(call)
        callees[call["source"]].append(call)
    targets = {s["id"] for s in found}
    label = {s["id"]: f"{s['path']}::{s['name']}" for s in found} if len(found) > 1 else {}
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
    direct = [_site(by_id, c["source"], c["line"], label.get(t)) for t in targets for c in callers[t]
              if c["source"] not in targets]
    outgoing = [_site(by_id, c["target"], c["line"], label.get(t)) for t in targets for c in callees[t]
                if c["target"] not in targets]
    files = {s["path"] for s in found}
    importers = sorted({d["source"] for d in data["dependencies"] if d.get("target") in files and d["source"] not in files})
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
                        "path::name to keep one." if label else ""),
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
