"""Facts about the shape of the project that an edit touches, for the edit hook: the import cycle an added import closes, and
the hotspot or the usual change partner an edited file has.

Both read only what is already cached, so the hook never waits for them: the analysis of the code graph (`code_graph.cached_graph`)
and the history `doc_insights.history` keeps in memory (`doc_insights.cached_history`). Nothing cached means nothing said:
no analysis starts and no `git` process runs. Only Python imports are mapped to project files.
"""
import ast
import collections

import code_graph
import code_health
import doc_insights
import index_profile
import index_scope

HOTSPOT_TOP = 10
HOTSPOT_MIN_COMMITS = 10
MAX_FINDINGS = 3


def _module_imports(rel, text, known):
    """The project files a Python text imports at module level, with the line of the first import of each.

    Parameters: rel (str) project-relative path of the file; text (str) its text; known (set of str) the project's files.
    Returns: dict {file: line}; empty when the text does not parse. An import inside a function or a class is left out: it is
    the usual way to break a cycle.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for _alias, target, _base in code_graph.python_import_targets(rel, node, known):
                if target and target != rel:
                    found.setdefault(target, node.lineno)
    return found


def _chain(edges, start, goal):
    """The shortest chain of files from `start` to `goal` over the import edges, or [] when `goal` cannot be reached."""
    previous, queue = {start: None}, collections.deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            chain = []
            while node is not None:
                chain.append(node)
                node = previous[node]
            return chain[::-1]
        for following in sorted(edges.get(node, ())):
            if following not in previous:
                previous[following] = node
                queue.append(following)
    return []


def added_cycles(root, rel, before, after):
    """The imports an edit adds to a Python file that close an import cycle in the cached code graph.

    Parameters: root (str) project folder; rel (str) project-relative path of the edited file; before (str or None) text
    before the edit; after (str) text after it.
    Returns: list of {"line", "imports", "chain"} (at most MAX_FINDINGS): the added import, the project file it names and the
    chain of files from that one back to `rel`. Empty when the graph is not cached, the file is not Python or no added
    import has a way back.
    """
    if not rel.endswith(".py"):
        return []
    data = code_graph.cached_graph(root)
    if data is None:
        return []
    known = {item["path"] for item in data.get("files") or []}
    edges = code_graph.import_edges(data)
    old = _module_imports(rel, before, known) if before else {}
    found = []
    for target, line in sorted(_module_imports(rel, after, known).items(), key=lambda item: item[1]):
        chain = [] if target in old else _chain(edges, target, rel)
        if chain:
            found.append({"line": line, "imports": target, "chain": chain})
    return found[:MAX_FINDINGS]


class _Indexed:
    """The part of a documentation model that `doc_insights.coupling` reads, over a cached analysis."""

    def __init__(self, data):
        self.graph = data
        self._files = [item["path"] for item in data.get("files") or []]

    def files(self):
        """Project-relative paths of the indexed files."""
        return self._files


def history_findings(root, rels):
    """The hotspots and the usual change partners among the files of an edit, from the cached history and graph.

    Parameters: root (str) project folder; rels (list of str) project-relative paths of every file in the edit.
    Returns: dict {"hotspots": [{"path", "commits", "rank"}], "coupled": [{"path", "with", "shared", "degree"}]}; a hotspot
    is a code file in the first HOTSPOT_TOP of the commits-times-lines ranking with at least HOTSPOT_MIN_COMMITS commits;
    a partner is a file outside the edit that `doc_insights.coupling` pairs with an edited one and that the edited one does
    not import. Both lists are empty when the history or the graph is not cached.
    """
    nothing = {"hotspots": [], "coupled": []}
    commits = doc_insights.cached_history(root)
    data = code_graph.cached_graph(root) if commits else None
    if data is None:
        return nothing
    lines = {item["path"]: item.get("lines") or 0 for item in data.get("files") or []}
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    ranking = doc_insights.ranked_hotspots(commits, lambda path: path in lines and index_profile.kind(path, profile) == "code",
                                           lines.get)
    edited = set(rels)
    hot = [{"path": path, "commits": count, "rank": rank} for rank, (path, count, _lines, _last)
           in enumerate(ranking[:HOTSPOT_TOP], 1) if path in edited and count >= HOTSPOT_MIN_COMMITS]
    coupled = code_health.partners(doc_insights.coupling(_Indexed(data), commits), edited)
    return {"hotspots": hot, "coupled": coupled[:MAX_FINDINGS]}
