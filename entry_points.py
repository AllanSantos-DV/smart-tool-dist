"""Where execution starts in a project, grouped by architecture layer: the functions the documentation treats as entry points
(`doc_scope`), the `__main__` blocks that run no function, the functions that carry a route annotation and the files a
`package.json` declares as `main` or `bin`.

The model is the read-only one `docs generate` starts from; nothing is generated or written.
"""
import ast
import json
import os
import posixpath
import re

import code_graph
import doc_cli
import doc_scope
import layers
import orphan_modules

DEFAULT_LIMIT = 50
ROUTE = re.compile(r"^[ \t]*(?:" + "|".join(re.escape(a) for name, _words, annotations in layers.CONVENTIONS
                                          if name == "entry" for a in annotations) + r")(?![\w.])")

RULE = ("kind `__main__` is a function a `__main__` block of a Python file runs; `__main__ block` is a block that runs no "
        "function of the project (the script itself); `main` is a function named main; `layer` is a public function of the "
        "highest declared layer; `root` is, when no layers are declared, a public function no code calls whose flow reaches "
        "at least 3 others; `route` is a function with a route or controller annotation (the entry annotations of `layers`: "
        "Spring, JAX-RS, Flask, FastAPI) on or above its definition; `package.json main` and `package.json bin` are the files "
        "a manifest declares. `flow` is the number of code functions reached from the entry through static calls (6 hops, up "
        "to 60) and `stamp` the hash of their code that `docs/scope` records, so a changed stamp means the feature's code "
        "changed. Static analysis: an entry point reached through a framework, a config file or a name built at run time may be "
        "missing; JavaScript `require.main` and Go `main` packages are read only through the function named main.")


def _routed(function, lines):
    """Whether a function carries a route annotation: a decorator, or an annotation line touching its definition (the lines
    above it, and the ones from its first line on when the language counts the annotations as part of the definition)."""
    if any(ROUTE.match(f"@{decorator}") for decorator in function.get("decorators", ())):
        return True
    first = function["start"] - 1
    above = first - 1
    while above >= 0 and lines[above].lstrip().startswith("@"):
        if ROUTE.match(lines[above]):
            return True
        above -= 1
    below = first
    while below < len(lines) and lines[below].lstrip().startswith("@"):
        if ROUTE.match(lines[below]):
            return True
        below += 1
    return False


def _function_rows(model):
    """The entry-point functions of the model with their kind, in file and line order: those `doc_scope` names plus every
    function that carries a route annotation."""
    launched, entries = set(model.launched()), set(model.entry_points())
    top = model.rule.layers[0]["name"] if model.rule else None
    rows = []
    for anchor, function in sorted(model.function_of.items(), key=lambda item: (item[1]["path"], item[1]["start"])):
        if model.kind[function["path"]] != "code":
            continue
        lines = (model.read(function["path"]) or "").splitlines()
        if _routed(function, lines):
            kind = "route"
        elif anchor not in entries:
            continue
        elif anchor in launched:
            kind = "__main__"
        elif function["name"].split(".")[-1] == "main":
            kind = "main"
        elif top and function["required"] and model.layer_of[function["path"]] == top:
            kind = "layer"
        else:
            kind = "root"
        rows.append({"kind": kind, "name": function["name"], "path": function["path"], "line": function["start"],
                     "anchor": anchor})
    return rows


def _block_rows(model, notes):
    """The `__main__` blocks of the Python code files that run no function of the project."""
    files = set(model.files())
    rows = []
    for path in sorted(files):
        source = model.read(path) if path.endswith(".py") and model.kind_of(path) == "code" else None
        if not source or "__main__" not in source:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError) as error:
            notes.append(f"{path} could not be parsed, so its __main__ block was not read: {error}")
            continue
        if doc_cli.launched(path, source, files):
            continue
        rows += [{"kind": "__main__ block", "name": None, "path": path, "line": node.lineno}
                 for node in tree.body if isinstance(node, ast.If) and doc_cli._is_main_guard(node.test)]
    return rows


def _candidates(folder, value):
    """The project paths a manifest entry can mean: the path itself, with a script extension, or as a folder's index."""
    base = posixpath.normpath(posixpath.join(folder, value.replace("\\", "/")))
    yield base
    yield from (base + ext for ext in orphan_modules.SCRIPT)
    yield from (f"{base}/index{ext}" for ext in orphan_modules.SCRIPT)


def _manifest_rows(model, notes):
    """The `main` and `bin` files of every package.json of the project that exist; a declared file that is missing, and a
    manifest that is not JSON, go to `notes`."""
    rows = []
    for manifest in sorted(p for p in model.files() if posixpath.basename(p) == "package.json"):
        try:
            data = json.loads(model.read(manifest) or "")
        except ValueError:
            notes.append(f"{manifest} is not valid JSON, so its main and bin were not read.")
            continue
        if not isinstance(data, dict):
            continue
        folder = posixpath.dirname(manifest)
        package = str(data.get("name") or posixpath.basename(folder) or "package").rsplit("/", 1)[-1]
        bins = data.get("bin")
        declared = [("package.json main", "main", data["main"])] if isinstance(data.get("main"), str) else []
        declared += [("package.json bin", package, bins)] if isinstance(bins, str) else \
            [("package.json bin", name, value) for name, value in sorted(bins.items()) if isinstance(value, str)] \
            if isinstance(bins, dict) else []
        for kind, name, value in declared:
            found = next((c for c in _candidates(folder, value) if os.path.isfile(os.path.join(model.root, *c.split("/")))), None)
            if found:
                rows.append({"kind": kind, "name": name, "path": found, "line": None})
            else:
                notes.append(f"{manifest} declares {value} as {kind.split()[-1]} {name!r}, which is not a file of the project.")
    return rows


def _layer(model, row):
    if not model.rule:
        return None
    if row["path"] in model.layer_of:
        return model.layer_of[row["path"]]
    return model.rule.assign(row["path"], model.read(row["path"]))


def _grouped(model, rows):
    """The rows in groups: one per declared layer in the declared order, then the rows of no layer."""
    names = [layer["name"] for layer in model.rule.layers] if model.rule else []
    groups = [{"layer": name, "entries": [r for r in rows if r["layer"] == name]} for name in names]
    groups.append({"layer": None, "entries": [r for r in rows if r["layer"] is None]})
    return [group for group in groups if group["entries"]]


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The entry points of the project, grouped by layer.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) rows returned (the flow of each is
    read, so the cost grows with it).
    Returns: dict {"layers": [{"layer": str or None (no layer), "entries": [{"kind", "name" (None for a `__main__ block`),
    "path", "line" (None for a manifest entry), "layer", "flow": int and "stamp": str for a function, None otherwise}]}],
    "counts": {"total": int, "by_kind": {kind: int}} (before `limit`), "notes": [str], "rule": str}.
    Raises: ValueError when the code graph is empty.
    """
    model = doc_scope._Model(root, view_id)
    if not model.graph.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    notes = []
    rows = _function_rows(model) + _block_rows(model, notes) + _manifest_rows(model, notes)
    for row in rows:
        row["layer"] = _layer(model, row)
    rows.sort(key=lambda r: (r["path"], r["line"] or 0, r["kind"]))
    kinds = {}
    for row in rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    shown = rows[:limit]
    for row in shown:
        anchor = row.pop("anchor", None)
        row["flow"] = len(model.flow(anchor)) if anchor else None
        row["stamp"] = doc_scope.flow_stamp(model, anchor) if anchor else None
    if model.roots_hidden:
        notes.append(f"{model.roots_hidden} more call-graph root(s) with a narrower flow were left out by the documentation's "
                     f"cap of {doc_scope.MAX_ROOTS}.")
    if model.graph.get("truncated"):
        notes.append(f"{code_graph.left_out_sentence(model.graph)} Entry points beyond the cut are not listed.")
    if len(rows) > len(shown):
        notes.append(f"{len(rows) - len(shown)} more entry point(s) were cut by limit.")
    return {"layers": _grouped(model, shown), "counts": {"total": len(rows), "by_kind": dict(sorted(kinds.items()))},
            "notes": notes, "rule": RULE}
