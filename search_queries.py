"""smart_search `queries`: several searches of one project in one call, one section per query in the answer.

Each query runs through the ordinary smart_search path with the arguments the call shares (project_root, group, top_k,
detail, layer, single_language_reason, decision_notes, and `folder` as the default), so a query is answered exactly as the
same search sent alone.
"""
import web_fetch_many

MAX_QUERIES = 5
QUERY_TEXTS = ("query_identifiers", "query_comments", "doc_query")
QUERY_FIELDS = (*QUERY_TEXTS, "folder")
LABEL_CHARS = 80


def expand(arguments):
    """The searches `arguments["queries"]` stands for, as `[(label, arguments of one smart_search call)]`, in order.

    Parameters: arguments (the smart_search arguments, `queries` present). Returns: the list. Raises: ValueError for
    `queries` that is not a list of 1 to MAX_QUERIES objects, a query with a field other than QUERY_FIELDS or without any
    query text, and a top-level query text next to `queries` (one form or the other, not both)."""
    queries = arguments["queries"]
    if not isinstance(queries, list) or not queries:
        raise ValueError(f"queries must be a list of 1 to {MAX_QUERIES} query objects")
    if len(queries) > MAX_QUERIES:
        raise ValueError(f"queries holds at most {MAX_QUERIES} searches per call, got {len(queries)}: split them in "
                         f"calls of {MAX_QUERIES} or fewer")
    clash = [name for name in QUERY_TEXTS if name in arguments]
    if clash:
        raise ValueError(f"queries replaces {', '.join(clash)}: send the searches either as queries or as the single "
                         "search fields, not both")
    shared = {key: value for key, value in arguments.items() if key != "queries"}
    expanded = []
    for number, query in enumerate(queries, 1):
        if not isinstance(query, dict):
            raise ValueError(f"queries[{number}] must be an object with {', '.join(QUERY_FIELDS)}")
        unknown = sorted(set(query) - set(QUERY_FIELDS))
        if unknown:
            raise ValueError(f"queries[{number}] does not take {', '.join(unknown)}: only {', '.join(QUERY_FIELDS)} "
                             "differ between the searches; the rest is set once for the call")
        field = next((name for name in QUERY_TEXTS if name in query), None)
        if field is None:
            raise ValueError(f"queries[{number}] needs one of {', '.join(QUERY_TEXTS)}")
        expanded.append((_label(field, query[field]), {**shared, **query}))
    return expanded


def _label(field, text):
    """`field: text` on one line, the text cut at LABEL_CHARS characters."""
    line = " ".join(str(text).split())
    return f"{field}: {line[:LABEL_CHARS]}{'...' if len(line) > LABEL_CHARS else ''}"


def render(results):
    """The text the agent reads: a count line, then one numbered section per query with its answer or `ERROR: reason`.

    Parameters: results ([{"label", "ok", "text" or "error"}], one per query, in order)."""
    return web_fetch_many.render_batch("smart_search", "queries", "answered", results, "label")
