"""The keys of the `DEFAULT_CONFIG` of a project's `config.py` against the README and `docs` text, in both directions: the
keys no doc names, and the keys a doc names in a config context that `DEFAULT_CONFIG` does not have. The config files the
code reads come from the scan `doc_extras` makes for its Configuration page and the docs are the ones `undocumented_env`
reads. Nothing is written.
"""
import ast
import json
import posixpath
import re

import code_health
import doc_extras
import env_unread
import project_scan
import undocumented_env

DEFAULT_LIMIT = 30
NOTE = ("Only the top-level keys of the DEFAULT_CONFIG dict literal of a tracked config.py are listed, and a key counts as "
        "documented when the git-tracked README or `docs` text (the generated docs/scope is left out) writes its name as a "
        "whole word with the same case. A key the docs name is read in a config context only: a snake_case name (with an "
        "underscore, so a single word is never taken for a key) in inline code, alone or as `key=value`, on a line that "
        "names a config file that the module holding DEFAULT_CONFIG reads or DEFAULT_CONFIG itself, and the top-level keys "
        "of a json block that follows such a line; the keys nested in DEFAULT_CONFIG count as known, and a key built at run time is not seen.")

_KEY = re.compile(r'"?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)"?(?:\s*[:=].*)?')
_CONTEXT = "DEFAULT_CONFIG"
_JSON_BLOCKS = {"json", "jsonc"}


def _default_config(root):
    """The top-level keys and every key of the `DEFAULT_CONFIG` dict literals of the tracked `config.py` files.

    Returns: tuple (rows, known): rows is a list of {"key", "path", "line"} in file order, known the set of all keys,
    nested ones included.
    Raises: ValueError when no tracked config.py holds such a dict.
    """
    rows, known = [], set()
    for rel, text in project_scan.tracked_texts(root, (".py",)):
        if text is None or posixpath.basename(rel) != "config.py":
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Dict) \
                    or not any(isinstance(t, ast.Name) and t.id == _CONTEXT for t in targets):
                continue
            rows += [{"key": key.value, "path": rel, "line": key.lineno} for key in node.value.keys
                     if isinstance(key, ast.Constant) and isinstance(key.value, str)]
            known.update(key.value for inner in ast.walk(node.value) if isinstance(inner, ast.Dict)
                         for key in inner.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
    if not rows:
        raise ValueError(f"No {_CONTEXT} dict literal in a tracked config.py of {root}: this report reads the keys of that dict.")
    return rows, known


def _block_keys(block):
    """The (key, line) of the top-level keys of a json block given as (line number, text) pairs; none when it is not an object."""
    try:
        data = json.loads("\n".join(text for _number, text in block))
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    return [(key, next((number for number, text in block if f'"{key}"' in text), block[0][0])) for key in data]


def _doc_keys(texts, markers):
    """The (key, path, line) of every key the docs name in a config context, in file and line order."""
    for rel, text in texts:
        fence, block, context, last = None, [], False, ""
        for number, line in enumerate(text.splitlines(), 1):
            marker = env_unread._FENCE.match(line)
            if marker:
                if fence is None:
                    fence, block, context = marker[1].lower() or "", [], any(name in last for name in markers)
                else:
                    if fence in _JSON_BLOCKS and context and block:
                        for key, at in _block_keys(block):
                            yield key, rel, at
                    fence = None
            elif fence is not None:
                block.append((number, line))
            else:
                if line.strip():
                    last = line
                if any(name in line for name in markers):
                    for span in env_unread._SPAN.findall(line):
                        found = _KEY.fullmatch(span.strip())
                        if found:
                            yield found[1], rel, number


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The `DEFAULT_CONFIG` keys no README or `docs` file names, and the keys the docs name in a config context it lacks.

    Parameters: root (str) project folder, a git repository; view_id (str or None) index view; limit (int) rows returned in
    each list.
    Returns: dict {"unread": [{"key", "path", "line"}] the top-level keys no doc names, in file order, "not_in_config":
    [{"key", "path", "line", "mentions"}] the keys the docs name that the dict lacks, by file and line, `path` and `line`
    the first doc site and `mentions` the count of doc sites, "counts": {"keys", "documented", "unread", "named_in_docs",
    "not_in_config", "docs_checked"}, "note": str}.
    Raises: ValueError when the code graph is empty, git cannot list the folder or no config.py holds a DEFAULT_CONFIG.
    """
    model = code_health._model(root, view_id)
    keys, known = _default_config(root)
    texts = undocumented_env.doc_texts(root)
    words = undocumented_env.words_of(texts)
    modules = {row["path"] for row in keys}
    files = {posixpath.basename(name.replace("\\", "/")) for name, sites in doc_extras.config_reads(model)["Files"].items()
             if any(site.rpartition(":")[0] in modules for site in sites)}
    named = {}
    for key, rel, line in _doc_keys(texts, files | {_CONTEXT}):
        named.setdefault(key, []).append((rel, line))
    unread = [row for row in keys if row["key"] not in words]
    absent = []
    for key, places in named.items():
        if key not in known:
            path, line = min(places)
            absent.append({"key": key, "path": path, "line": line, "mentions": len(places)})
    absent.sort(key=lambda row: (row["path"], row["line"], row["key"]))
    counts = {"keys": len(keys), "documented": len(keys) - len(unread), "unread": len(unread), "named_in_docs": len(named),
              "not_in_config": len(absent), "docs_checked": len(texts)}
    return {"unread": unread[:limit], "not_in_config": absent[:limit], "counts": counts, "note": NOTE}
