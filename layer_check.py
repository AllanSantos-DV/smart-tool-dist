"""Layer rule of the project applied at the moment a function is written: a call that goes up the declared layers.

The rule is the one confirmed in `.smart-tool/layers.json` (`layers`); a project without it is never checked. A call is
recognised by the name it names, so only a function name that is unique among the code files of the project and not
defined in the edited file counts, and only a call the edit adds (the name is not called in the same function before it).
"""
import re

import code_graph
import doc_check
import layers

_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_COMMENT = re.compile(r"^\s*(#|//|\*|/\*)")
MAX_FINDINGS = 5


def _called(source, function):
    """Names called in the lines of one function of `source`."""
    lines = source.splitlines()[function["start"] - 1:function["end"]]
    return {name for line in lines if not _COMMENT.match(line) for name in _CALL.findall(line)} - {function["name"]}


def review(root, rel, before, after, edited):
    """The calls an edit adds that go from a lower layer to a higher one.

    Parameters: root (str) project folder; rel (str) project-relative path of the edited file; before (str or None) text
    before the edit; after (str) text after it; edited (dict) `doc_check.written` of the same edit.
    Returns: list of dicts {"function", "line", "called", "path", "from", "to"}, empty when the project declares no layers
    or the file belongs to none. Raises: ValueError when the layers file is unreadable; RuntimeError when the graph is.
    """
    rule = layers.load(root)
    if rule is None:
        return []
    source_layer = rule.assign(rel, after)
    if source_layer is None:
        return []
    order = {layer["name"]: index for index, layer in enumerate(rule.layers)}
    owners = {}
    for symbol in code_graph.build_current(root, None, shared=True).get("symbols") or []:
        owners.setdefault(symbol["name"].split(".")[-1], set()).add(symbol["path"])
    old = {function["name"]: function for function in doc_check.functions(rel, before)} if before else {}
    found = []
    for function in edited["touched"]:
        previous = old.get(function["name"])
        known = _called(before, previous) if previous else set()
        for name in sorted(_called(after, function) - known):
            paths = owners.get(name, set())
            if len(paths) != 1 or rel in paths:
                continue
            path = next(iter(paths))
            target_layer = rule.assign(path)
            if target_layer and order[target_layer] < order[source_layer]:
                found.append({"function": function["name"], "line": function["start"], "called": name, "path": path,
                              "from": source_layer, "to": target_layer})
    return found[:MAX_FINDINGS]

