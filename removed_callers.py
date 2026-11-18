"""Callers that still name a function an edit removed or renamed, or whose signature it changed.

The functions the text before the edit defines and the text after lacks (`doc_check.functions`) are looked up in the code
graph: calls the graph resolved to them (a graph of the text before the edit, as in the edit hook), calls it left
unresolved under that name (a graph of the text after the edit, as in a diff already on disk), imports of the name, and
the calls still in the edited file. Names are compared by their last segment, so a name the file still defines
elsewhere (a method of another class) is not reported. For a function that stays with another signature the callers are
the calls from other files, resolved or unresolved by name; imports and calls in the edited file are left out.
"""
import posixpath
import re

import code_graph
import code_impact
import doc_check
import doc_scope

MAX_CALLERS = 5
MAX_SITES = 200
UNRESOLVED_CAP = 1000
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_COMMENT = re.compile(r"^\s*(#|//|\*|/\*)")


def _short(name):
    return name.rpartition(".")[2]


def _gone(rel, before, after):
    """Functions of `before` whose last name segment the functions of `after` no longer carry: {short name: qualified name}."""
    kept = {_short(function["name"]) for function in doc_check.functions(rel, after)}
    old = {}
    for function in doc_check.functions(rel, before):
        short = _short(function["name"])
        if short not in kept and not (short.startswith("__") and short.endswith("__")):
            old.setdefault(short, function["name"])
    return old


def _resigned(rel, before, after):
    """Qualified names of the functions both texts define once whose signature differs; a name a text defines more than once
    (overloads) is not compared."""
    def hashes(text):
        found = {}
        for function in doc_check.functions(rel, text):
            found.setdefault(function["name"], []).append(doc_scope.signature_hash(function["signature"]))
        return found
    old, new = hashes(before), hashes(after)
    return [name for name, signed in old.items()
            if len(signed) == 1 and len(new.get(name, ())) == 1 and new[name] != signed
            and not (_short(name).startswith("__") and _short(name).endswith("__"))]


def _names(expression, short, stem, method):
    """Whether a call expression can mean the removed function: its last name is the function's, and for a plain function
    it is bare or reached through the module (`stem.name`); a method is reached through any receiver."""
    parts = _IDENTIFIER.findall(expression)
    return bool(parts) and parts[-1] == short and (len(parts) == 1 or method or parts[-2] == stem)


def _sites(root, data, rel, name, short, after, removed=True):
    """The (path, line) pairs that reference one function, from the graph; for a removed function also the imports of its
    name and the calls left in the edited file (`after`)."""
    method, stem = "." in name, posixpath.splitext(posixpath.basename(rel))[0]
    sites = set()
    try:
        found = code_impact.impact(root, f"{rel}::{name}", limit=MAX_SITES)
        sites.update((site["path"], site["line"]) for site in found["callers"] if site["path"] != rel)
    except ValueError:
        pass
    sites.update((call["path"], call["line"]) for call in data.get("unresolved") or []
                 if call["path"] != rel and _names(call["expression"], short, stem, method))
    if not removed:
        return sorted(sites)
    sites.update((dep["source"], dep["line"]) for dep in data.get("dependencies") or []
                 if dep.get("target") == rel and dep["source"] != rel and _names(dep.get("specifier") or "", short, stem, False))
    called = re.compile(rf"\b{re.escape(short)}\s*\(")
    sites.update((rel, number) for number, line in enumerate(after.splitlines(), 1)
                 if called.search(line) and not _COMMENT.match(line))
    return sorted(sites)


def _note(data):
    parts = []
    cut = code_graph.left_out_sentence(data)
    if cut:
        parts.append(f"{cut} Callers beyond the cut are not listed.")
    if len(data.get("unresolved") or []) >= UNRESOLVED_CAP:
        parts.append(f"The graph keeps at most {UNRESOLVED_CAP} unresolved calls and this one reached it, so calls by name "
                     "beyond them may be missing.")
    if not data.get("symbols") and not data.get("dependencies"):
        parts.append("The code graph is empty: index the project (project_manage action=index) to find the callers.")
    return " ".join(parts)


def review(root, rel, before, after):
    """The callers that still reference the functions an edit removed or renamed in one file.

    Parameters: root (str) project folder; rel (str) project-relative path of the edited file; before (str or None) text
    before the edit; after (str) text after it.
    Returns: dict {"removed": [qualified names], "findings": [{"name", "path", "callers": [{"path", "line"}] (up to
    MAX_CALLERS), "total", "also_defined_in": [paths]}], "note": str}; `note` says what the graph cut, "" when it cut
    nothing. The graph is read only when a function is gone; findings hold the removed functions something still calls.
    Raises: RuntimeError when the graph cannot be built, ValueError when the project has no index.
    """
    gone = _gone(rel, before, after) if before and doc_check.supported(rel) else {}
    if not gone:
        return {"removed": [], "findings": [], "note": ""}
    return {"removed": list(gone.values()), **_findings(root, rel, list(gone.items()), after, True)}


def _findings(root, rel, functions, after, removed):
    """The callers of each (short name, qualified name) pair, as `review` and `signature_review` return them."""
    data = code_graph.build_current(root, None, shared=True)
    defined = {}
    for symbol in data.get("symbols") or []:
        defined.setdefault(_short(symbol["name"]), set()).add(symbol["path"])
    findings = []
    for short, name in functions:
        sites = _sites(root, data, rel, name, short, after, removed)
        if sites:
            findings.append({"name": name, "path": rel, "callers": [{"path": p, "line": n} for p, n in sites[:MAX_CALLERS]],
                             "total": len(sites), "also_defined_in": sorted(defined.get(short, set()) - {rel})})
    return {"findings": findings, "note": _note(data)}


def signature_review(root, rel, before, after):
    """The callers in other files of the functions an edit left with another signature (parameters, order, defaults, return
    annotation) in one file.

    Parameters: as `review`.
    Returns: dict {"changed": [qualified names], "findings": as `review`, "note": str}; the callers are those outside
    `rel`. The graph is read only when a signature changed.
    Raises: RuntimeError when the graph cannot be built, ValueError when the project has no index.
    """
    changed = _resigned(rel, before, after) if before and doc_check.supported(rel) else []
    if not changed:
        return {"changed": [], "findings": [], "note": ""}
    return {"changed": changed, **_findings(root, rel, [(_short(name), name) for name in changed], after, False)}


def sites_text(finding):
    """`app/api.py:6, app/cli.py:3 (+2 more)` for one finding."""
    shown = ", ".join(f"{site['path']}:{site['line']}" for site in finding["callers"])
    more = f" (+{finding['total'] - len(finding['callers'])} more)" if finding["total"] > len(finding["callers"]) else ""
    return shown + more
