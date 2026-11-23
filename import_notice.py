"""The imports an edit adds to a Python file that no manifest of its folder declares, for the edit hook and the review of a
finished diff: the file's own added imports judged by `unused_deps.undeclared_in_file` against the nearest manifest.

Like `edit_shape`, it reads only the cached code graph (for the names of the project's own modules), so nothing cached means
nothing said and no analysis starts. Only Python files are read: the imports of JS and TS files come from a Node analyzer too
slow for a hook.
"""
import ast

import code_graph
import unused_deps


def _imports(text):
    """The top-level module of every absolute import in a Python text, with the line of the first one; empty when the text does
    not parse. Imports inside functions and `try` blocks count."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if node.lineno < found.get(top, node.lineno + 1):
                found[top] = node.lineno
    return found


def added_undeclared(root, rel, before, after):
    """The imports an edit adds to a Python file that the nearest manifest of its folder does not declare.

    Parameters: root (str) project folder; rel (str) project-relative path of the edited file; before (str or None) text
    before the edit; after (str) text after it.
    Returns: list of `unused_deps.undeclared_in_file` items, one per added top-level module, by line; every one carries a
    `confidence` and a `reason`, never certainty. Empty when the file is not Python, the graph is not cached, the text does
    not parse, no import was added or no manifest of the folder exists.
    Raises: ValueError when the nearest manifests exist and none can be read; OSError when a folder cannot be listed.
    """
    if not rel.endswith(".py"):
        return []
    data = code_graph.cached_graph(root)
    if data is None:
        return []
    old = _imports(before) if before else {}
    added = {name: line for name, line in _imports(after).items() if name not in old}
    if not added:
        return []
    paths = [item["path"] for item in data.get("files") or []]
    return unused_deps.undeclared_in_file(root, rel, dict(sorted(added.items(), key=lambda item: item[1])), paths)
