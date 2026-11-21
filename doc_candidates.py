"""Feature candidates from the embeddings home for public functions no entry point reaches through the call graph.

The graph links a function to a feature when a call path exists; for a public function with no path at all (code the
framework calls, configuration, a handler), the cosine between its text (name, signature, first docstring line) and
each feature's text proposes at most MAX_CANDIDATES features. They are recorded as status `candidate` with their
score and shown as "possible" on both sides of the chain; the embedder never confirms a link (measured: right on the
first place 5 times in 11, margins of 0.01 to 0.03 between first and second).
"""
import math

import local_embedder

MAX_CANDIDATES = 2
MAX_FLOW_NAMES = 15


def orphans(model, reached):
    """Public code functions that are not an entry point and that no entry point's flow reaches.

    Parameters: model (doc_scope._Model); reached (dict anchor -> features, from doc_scope.features).
    """
    return [anchor for anchor in model.public() if anchor not in reached]


def _function_text(model, anchor):
    function = model.function_of[anchor]
    return " ".join(part for part in (function["name"], function["signature"], function["doc"]) if part)


def _feature_text(model, entry):
    names = [model.function_of[a]["name"] for a in model.flow(entry)[:MAX_FLOW_NAMES]]
    return _function_text(model, entry) + (" uses " + ", ".join(names) if names else "")


def _cosine(a, b):
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def propose(model, reached, skip=()):
    """Candidate link rows for the orphan functions not in `skip` (those that already have candidates).

    Returns: list of link dicts (origin embedder, status candidate, score, hops None); [] when there is nothing to
    propose or no embeddings home is installed. Raises RuntimeError when the home is announced but does not answer, and
    lets the home's own errors through: a candidate is never invented without vectors.
    """
    todo = [anchor for anchor in orphans(model, reached) if anchor not in set(skip)]
    entries = model.entry_points()
    if not todo or not entries or not local_embedder.configured():
        return []
    info = local_embedder.info()
    if not info:
        raise RuntimeError("The embeddings home is announced but does not answer; no feature candidate was proposed.")
    vectors = local_embedder.embed(info["model"], [_function_text(model, a) for a in todo]
                                   + [_feature_text(model, e) for e in entries])
    functions, features = vectors[:len(todo)], vectors[len(todo):]
    rows = []
    for anchor, vector in zip(todo, functions):
        ranked = sorted(((_cosine(vector, feature), entry) for entry, feature in zip(entries, features)), reverse=True)
        rows += [{"feature_id": entry, "fn_id": anchor, "layer": model.layer_of.get(model.function_of[anchor]["path"]),
                  "hops": None, "origin": "embedder", "score": round(score, 4), "status": "candidate"}
                 for score, entry in ranked[:MAX_CANDIDATES]]
    return rows
