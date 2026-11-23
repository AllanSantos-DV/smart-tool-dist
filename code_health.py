"""Questions about the shape of a project that the code graph and the git history answer without writing documentation:
which code files are the riskiest to change, which functions nothing calls, which imports form a cycle and which
comments sit where they should not.

The same tables the `docs/scope` pages hold (`doc_insights`), asked directly by `project_manage`; nothing is written.
"""
import posixpath

import code_graph
import comment_check
import doc_check
import doc_insights
import doc_scope
import removed_callers

DEFAULT_LIMIT = 25


def _model(root, view_id):
    model = doc_scope._Model(root, view_id)
    if not model.graph.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    return model


def hotspots(root, view_id=None, limit=DEFAULT_LIMIT):
    """Code files ranked by the commits that touched them times their lines.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) rows returned.
    Returns: dict {"files": [{"path", "commits", "lines", "score", "last_change"}], "total": int, "commits_read": int,
    "note": str}; `note` is the reason no history could be read (not a git repository), otherwise "".
    Raises: ValueError when the code graph is empty.
    """
    model = _model(root, view_id)
    commits, note = doc_insights.history(root)
    rows = [] if note else doc_insights.hotspots(model, commits)
    files = [{"path": path, "commits": count, "lines": lines, "score": count * lines, "last_change": doc_insights._date(last)}
             for path, count, lines, last in rows[:limit]]
    return {"files": files, "total": len(rows), "commits_read": len(commits), "note": note or ""}


def _mention_index(graph):
    """The unresolved calls and the imports of a graph, by the last identifier of what they name: {name: [(path, text,
    target)]}; `target` is the file an import resolves to, None for a call."""
    found = {}
    for call in graph.get("unresolved") or []:
        names = removed_callers._IDENTIFIER.findall(call["expression"])
        if names:
            found.setdefault(names[-1], []).append((call["path"], call["expression"], None))
    for dep in graph.get("dependencies") or []:
        names = removed_callers._IDENTIFIER.findall(dep.get("specifier") or "")
        if names and dep.get("target"):
            found.setdefault(names[-1], []).append((dep["source"], dep["specifier"], dep["target"]))
    return found


def _name_mentions(index, function):
    """How many unresolved calls and import specifiers outside the function's file still name it (the graph could not tie
    them to a definition, so they may be calls to it)."""
    rel, short = function["path"], removed_callers._short(function["name"])
    stem, method = posixpath.splitext(posixpath.basename(rel))[0], "." in function["name"]
    return sum(1 for path, text, target in index.get(short, ())
               if path != rel and (target is None or target == rel) and removed_callers._names(text, short, stem, method and target is None))


def dead_code(root, view_id=None, limit=DEFAULT_LIMIT):
    """Functions nothing calls and functions only tests call, from the static call graph.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) entries per list.
    Returns: dict {"no_caller": [...], "only_tests": [...], "counts": {"no_caller", "only_tests"}, "note": str,
    "graph_note": str}; each entry is {"name", "path", "line", "public", "name_mentions"}; `name_mentions` counts the unresolved calls
    and import specifiers of other files that still name the function, a lead that something may call it (the lists are
    not filtered by it); `graph_note` says what the analysis limits cut from
    the graph, "" when they cut nothing. Candidates only: a call through a framework, a decorator registration,
    reflection or a name built at run time is not seen.
    Raises: ValueError when the code graph is empty.
    """
    model = _model(root, view_id)
    alone, tested = doc_insights.dead_code(model, model.entry_points())
    index = _mention_index(model.graph)

    def entry(anchor):
        function = model.function_of[anchor]
        return {"name": function["name"], "path": function["path"], "line": function["start"],
                "public": bool(function["required"]), "name_mentions": _name_mentions(index, function)}

    return {"no_caller": [entry(a) for a in alone[:limit]], "only_tests": [entry(a) for a in tested[:limit]],
            "counts": {"no_caller": len(alone), "only_tests": len(tested)},
            "note": "Candidates from the static call graph (Python and Go only): read each one before deleting it.",
            "graph_note": code_graph.left_out_sentence(model.graph) or ""}


def coupled_with(root, paths, view_id=None, limit=DEFAULT_LIMIT):
    """Files that usually change in the same commits as the given ones, left out of the given set.

    Parameters: root (str) project folder; paths (list of str) project-relative changed files; view_id (str or None) index
    view; limit (int) rows returned. Pairs where one file imports the other are skipped: the call graph already shows them.
    Returns: dict {"files": [{"path", "with", "shared", "degree"}], "note": str}, strongest first, one row per partner;
    degree is a percentage; `note` is the reason no history could be read, otherwise "".
    Raises: ValueError when the code graph is empty.
    """
    model = _model(root, view_id)
    commits, note = doc_insights.history(root)
    if note:
        return {"files": [], "note": note}
    return {"files": partners(doc_insights.coupling(model, commits), set(paths))[:limit], "note": ""}


def partners(pairs, changed):
    """The files that change with the changed ones, from the pairs `doc_insights.coupling` returns.

    Parameters: pairs (list) rows (a, b, shared, degree, linked), strongest first; changed (set of str) the changed files.
    Returns: list of {"path", "with", "shared", "degree"}, strongest first, one row per partner outside `changed`; a pair
    where one file imports the other is skipped.
    """
    rows, seen = [], set()
    for first, second, shared, degree, linked in pairs:
        if linked or (first in changed) == (second in changed):
            continue
        partner, anchor = (second, first) if first in changed else (first, second)
        if partner not in seen:
            seen.add(partner)
            rows.append({"path": partner, "with": anchor, "shared": shared, "degree": int(degree * 100)})
    return rows


def _pop_component(stack, on_stack, node):
    """Members of the component rooted at `node`, taken off the Tarjan stack."""
    group = []
    member = None
    while member != node:
        member = stack.pop()
        on_stack.discard(member)
        group.append(member)
    return sorted(group)


def _components(edges):
    """Strongly connected components of size above one, and self-loops, of a directed graph (Tarjan, no recursion).

    Parameters: edges (dict) node -> set of the nodes it points to.
    Returns: list of sorted node lists.
    """
    index, low, stack, on_stack, found = {}, {}, [], set(), []
    for start in sorted(edges):
        if start in index:
            continue
        index[start] = low[start] = len(index)
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(sorted(edges[start])))]
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = len(index)
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges[child]))))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                group = _pop_component(stack, on_stack, node)
                if len(group) > 1 or node in edges[node]:
                    found.append(group)
    return found


def import_cycles(root, view_id=None, limit=DEFAULT_LIMIT):
    """Groups of files that import each other, directly or through a chain.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) cycles returned.
    Returns: dict {"cycles": [{"files": [...], "size": int}], "total": int, "files_in_cycles": int, "note": str}, the
    largest cycle first; only imports the graph resolved to a file of the project count.
    Raises: ValueError when the code graph is empty.
    """
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("symbols") and not data.get("dependencies"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    cycles = sorted(_components(code_graph.import_edges(data)), key=lambda files: (-len(files), files))
    note = code_graph.left_out_sentence(data) if data.get("truncated") else ""
    return {"cycles": [{"files": files, "size": len(files)} for files in cycles[:limit]], "total": len(cycles),
            "files_in_cycles": sum(len(files) for files in cycles), "note": note}


def comments(root, view_id=None, limit=DEFAULT_LIMIT):
    """Comments in the wrong place across the code files of the project: commented-out code, history tags, loose
    documentation and long inline blocks (`comment_check`), for files written before the edit hook existed.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) findings returned.
    Returns: dict {"findings": [{"path", "line", "issue", "name", "text"}], "total": int, "by_issue": {issue: count},
    "files_checked": int}.
    Raises: ValueError when the code graph is empty.
    """
    model = _model(root, view_id)
    findings, checked = [], 0
    for path in model.files():
        if model.kind_of(path) != "code" or not doc_check.supported(path):
            continue
        text = model.read(path)
        if not text:
            continue
        checked += 1
        findings += [{"path": path, **item} for item in comment_check.review(path, None, text)]
    by_issue = {}
    for item in findings:
        by_issue[item["issue"]] = by_issue.get(item["issue"], 0) + 1
    return {"findings": findings[:limit], "total": len(findings), "by_issue": by_issue, "files_checked": checked}
