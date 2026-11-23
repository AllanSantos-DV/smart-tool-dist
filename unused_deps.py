"""Dependencies the manifests declare that no code imports, and the imports no manifest declares, read from the external
imports of the code graph and the dependency tables `dependency_notice` already parses (package.json, requirements*.txt,
pyproject.toml, go.mod). Nothing is edited. A package name is not always the name the code imports it by, so every
finding says how far to trust it (`confidence`, `reason`) and, for Python, which names it may be an alias of.
"""
import json
import os
import posixpath
import re
import sys
from collections import defaultdict

import code_graph
import dependency_notice
import upgrade_preview

DEFAULT_LIMIT = 30
MAX_FILES_SHOWN = 3
MAX_CANDIDATES = 5
LANGUAGES = {"npm": "js", "PyPI": "python", "pyproject": "python", "Go": "go"}
NOTE = ("Only imports of the files the code graph read (Python, Go, JS and TS inside the index scope) are matched, folder by "
        "folder: a file belongs to the nearest folder holding a manifest of its language. Only package.json, "
        "requirements*.txt, pyproject.toml `project` tables and go.mod are read. A package that a tool, a config file or a "
        "dynamic import loads by name, or one imported under another name than its package name (PyYAML as yaml), is not "
        "seen as used: `confidence` says how far a finding can be trusted and `reason` why. Imports that are not package "
        "names (path aliases such as @/x), the standard library, Node core modules and names that are files or folders of "
        "the project are not reported as undeclared.")
_EXTENSIONS = {
    ".py": "python", ".go": "go",
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"), "js"),
}
_NPM_NAME = re.compile(r"(?:@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*")
_NODE_CORE = frozenset((
    "assert buffer child_process cluster console constants crypto dgram diagnostics_channel dns domain events fs http http2 "
    "https inspector module net os path perf_hooks process punycode querystring readline repl stream string_decoder sys "
    "timers tls trace_events tty url util v8 vm wasi worker_threads zlib").split())
_NPM_LOADED = re.compile(r"(^|[-/])(plugin|preset|loader|config|polyfill)s?([-/]|$)")
_PYTHON_TOOL = re.compile(r"(?:pytest|black|flake8|mypy|ruff|pylint|isort|coverage|tox|pre-commit|wheel|setuptools|twine|"
                          r"build|sphinx|bandit)(?:-.*)?")
_CUT = " The code graph was cut by its limits, so a file that imports it may be missing from it."


def _npm_root(specifier):
    """The package an npm import specifier names (`@scope/name` or `name`), or None when it is not a package name."""
    parts = specifier.split("/")
    root = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
    return root if _NPM_NAME.fullmatch(root) else None


def _root(specifier, language):
    """The part of an import specifier that names the package: the top-level module of Python, the package of npm, the whole
    import path of Go; None for a relative import, a Node `node:` module or a name that cannot be a package."""
    if specifier.startswith(".") or specifier.startswith("node:"):
        return None
    if language == "python":
        return specifier.split(".")[0] or None
    return _npm_root(specifier) if language == "js" else specifier


def _builtin(root, language):
    """Whether a package root is the standard library (Python), a Node core module or the standard library of Go (an import
    path whose first element has no dot)."""
    if language == "python":
        return root in sys.stdlib_module_names
    if language == "js":
        return root in _NODE_CORE
    return "." not in root.split("/")[0]


def _owner(path, folders):
    """The deepest of `folders` (project-relative, "" for the root) that holds `path`, or None."""
    folder = posixpath.dirname(path)
    held = [f for f in folders if not f or folder == f or folder.startswith(f + "/")]
    return max(held, key=len, default=None)


def _local_names(paths, folder):
    """Every folder name and file stem of `paths` below `folder`: an import of one is the project's own module or alias."""
    names = set()
    for path in paths:
        parts = path[len(folder) + 1 if folder else 0:].split("/")
        names.update(parts[:-1])
        names.add(parts[-1].rsplit(".", 1)[0])
    return names


def _go_indirect(text):
    """The modules a go.mod text marks `// indirect`."""
    found = set()
    for line in text.splitlines():
        tokens = line.split("//")[0].split()
        if "// indirect" in line and tokens:
            found.add(tokens[1] if tokens[0] == "require" and len(tokens) > 1 else tokens[0])
    return found


def _manifest(root, rel):
    """Reads one manifest.

    Returns: tuple (dict with `path`, `key`, `language`, `folder`, `pairs` (list of (name, specifier)), `indirect` (set of
    modules go.mod marks `// indirect`) and `scripts` (str, the npm scripts joined) when it is read, else None; str reason it
    was not read, "" when it was).
    """
    key = dependency_notice.ecosystem_of(rel)
    spec = dependency_notice._ECOSYSTEMS[key]
    if key not in LANGUAGES:
        return None, f"{spec.file} is not analyzed: the code graph does not read its language"
    try:
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as stream:
            text = stream.read()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"not read ({type(exc).__name__})"
    pairs = spec.parse(text)
    if pairs is None:
        return None, f"not valid {spec.syntax}"
    if key == "pyproject" and "[tool.poetry" in text:
        return None, "Poetry dependency tables are not read, only `project.dependencies` and `project.optional-dependencies`"
    scripts = (json.loads(text).get("scripts") if key == "npm" else None) or {}
    return {"path": rel, "key": key, "language": LANGUAGES[key], "folder": posixpath.dirname(rel),
            "pairs": list(dict.fromkeys(pairs)), "indirect": _go_indirect(text) if key == "Go" else set(),
            "scripts": " ".join(value for value in scripts.values() if isinstance(value, str)) if isinstance(scripts, dict)
            else ""}, ""


def _tooling(manifest, name):
    """Why an unused package is probably not meant to be imported (a type package, a command-line tool, a plugin a
    configuration loads, a Go indirect requirement), or "" when nothing says so."""
    key = manifest["key"]
    if key == "npm":
        if name.startswith("@types/"):
            return "type declarations: the compiler reads them, no import names them"
        if re.search(rf"(?<![\w@/.-]){re.escape(name)}(?![\w/.-])", manifest["scripts"]):
            return "named in a package.json script: a command-line tool, not an import"
        if _NPM_LOADED.search(name):
            return "a plugin, preset, loader or config that a configuration file loads by name, not by import"
    elif key == "Go":
        if name in manifest["indirect"]:
            return "marked `// indirect` in go.mod: another dependency requires it, this module's code does not import it"
    elif _PYTHON_TOOL.fullmatch(re.sub(r"[-_.]+", "-", name).lower()):
        return "a command-line tool or a plugin of one, run rather than imported"
    return ""


def _unused(scope, roots, undeclared_names, cut):
    """The declared packages of one folder that no import matches.

    Returns: list of dicts `manifest`, `ecosystem`, `name`, `specifier`, `confidence`, `reason`, plus `alias_candidates` for a
    Python package that may be imported under one of the names no manifest declares.
    """
    found = []
    for manifest in scope["manifests"]:
        for name, specifier in manifest["pairs"]:
            entry = {"key": manifest["key"], "name": name}
            if any(upgrade_preview._imports(root, entry) for root in roots):
                continue
            tool = _tooling(manifest, name)
            item = {"manifest": manifest["path"], "ecosystem": dependency_notice._ECOSYSTEMS[manifest["key"]].osv,
                    "name": name, "specifier": specifier, "tool": tool}
            if tool:
                item.update(confidence="low", reason=tool)
            elif manifest["language"] == "python" and undeclared_names:
                item.update(confidence="low", alias_candidates=undeclared_names[:MAX_CANDIDATES],
                            reason="no import has this name, but the code imports names no manifest declares and the import "
                                   "name of a package can differ from its package name")
            elif manifest["language"] == "python":
                item.update(confidence="medium", reason="every import of this folder is explained by a declared package, "
                                                        "none by this one")
            else:
                item.update(confidence="medium", reason="no import of the package was found; a dynamic import or a tool "
                                                        "that loads it by name is not seen")
            if cut:
                item.update(confidence="low", reason=item["reason"] + _CUT)
            found.append(item)
    return found


def _undeclared(scope, roots, local):
    """The imports of one folder that no declared package provides.

    Returns: list of (root, sorted files) in file-count order; the standard library, Node core modules and the names of
    the project's own files and folders are left out.
    """
    entries = [{"key": m["key"], "name": name} for m in scope["manifests"] for name, _ in m["pairs"]]
    found = []
    for root, files in roots.items():
        if _builtin(root, scope["language"]) or scope["language"] != "go" and root in local:
            continue
        if not any(upgrade_preview._imports(root, entry) for entry in entries):
            found.append((root, sorted(files)))
    return sorted(found, key=lambda item: (-len(item[1]), item[0]))


def _undeclared_item(scope, root, files, unused_names):
    """One undeclared import with the confidence and the reason to trust it."""
    item = {"folder": scope["folder"] or ".", "language": scope["language"], "name": root, "files": files[:MAX_FILES_SHOWN],
            "count": len(files)}
    if scope["incomplete"]:
        item.update(confidence="low", reason="a manifest of this folder was not read (see manifests_skipped) and may declare it")
    elif scope["language"] == "python" and unused_names:
        item.update(confidence="low", alias_candidates=unused_names[:MAX_CANDIDATES],
                    reason="no declared package has this name, but the declared packages listed in alias_candidates are never "
                           "imported and may provide it under this import name")
    elif scope["language"] == "python":
        item.update(confidence="medium", reason="no declared package provides it; it may arrive as a dependency of another "
                                                "package or be declared where this reading does not look (setup.py, Poetry)")
    elif scope["language"] == "js":
        item.update(confidence="medium", reason="a parent workspace may declare it (hoisted), another package may bring it, or "
                                                "it is a path alias")
    else:
        item.update(confidence="medium", reason="a replace directive, a vendor folder or a go.work may provide it")
    return item


def _scopes(manifests, skipped_keys):
    """The manifests grouped by (language, folder); `incomplete` marks a folder where a manifest of that language was skipped."""
    scopes = {}
    for manifest in manifests:
        scopes.setdefault((manifest["language"], manifest["folder"]),
                          {"language": manifest["language"], "folder": manifest["folder"], "manifests": [],
                           "incomplete": False})["manifests"].append(manifest)
    for key in skipped_keys:
        if key in scopes:
            scopes[key]["incomplete"] = True
    return scopes


def _nearest_scope(root, rel, language):
    """The manifests of `language` in the folder nearest to `rel` (its own folder, then each parent up to the project root).

    Returns: the scope dict `_scopes` builds, or None when no folder on the way holds a manifest of that language.
    Raises: ValueError when the nearest folder holds manifests of the language and none of them can be read.
    """
    folder = posixpath.dirname(rel)
    while True:
        here = os.path.join(root, *folder.split("/")) if folder else root
        names = [name for name in sorted(os.listdir(here) if os.path.isdir(here) else ())
                 if LANGUAGES.get(dependency_notice.ecosystem_of(name)) == language]
        if names:
            manifests, skipped = [], []
            for name in names:
                found, reason = _manifest(root, posixpath.join(folder, name))
                if found:
                    manifests.append(found)
                else:
                    skipped.append(f"{posixpath.join(folder, name)}: {reason}")
            if not manifests:
                raise ValueError(f"No manifest to analyze for {rel}: {'; '.join(skipped)}.")
            return {"language": language, "folder": folder, "manifests": manifests, "incomplete": bool(skipped)}
        if not folder:
            return None
        folder = posixpath.dirname(folder)


def undeclared_in_file(root, rel, specifiers, paths):
    """The imports of one file that no package of its nearest manifest provides.

    Parameters: root (str) project folder; rel (str) project-relative path of the file; specifiers (dict) import specifier ->
    line, the imports to judge; paths (iterable of str) the files of the project, whose names are the project's own
    modules and are never reported. Relative imports, the standard library and Node core modules are left out as in `report`.
    Returns: list of {"folder", "language", "name", "files", "count", "line", "manifests", "confidence", "reason"} (the
    items of `report`'s `undeclared`, without `alias_candidates`: that needs every import of the folder), in the order the
    specifiers are given; empty when the file's language has no manifest between it and the project root.
    Raises: ValueError for a Go file (an import of the module's own packages cannot be told from an external one without
    the graph) and when the nearest manifests of the language cannot be read; OSError when a folder cannot be listed.
    """
    language = _EXTENSIONS.get(posixpath.splitext(rel)[1])
    if language == "go":
        raise ValueError(f"{rel}: Go imports of the module's own packages cannot be told from external ones without the code graph.")
    scope = _nearest_scope(root, rel, language) if language else None
    if scope is None:
        return []
    roots, lines = {}, {}
    for specifier, line in specifiers.items():
        name = _root(specifier, language)
        if name:
            roots.setdefault(name, {rel})
            lines.setdefault(name, line)
    folder = scope["folder"]
    local = _local_names([path for path in paths if _EXTENSIONS.get(posixpath.splitext(path)[1]) == language
                          and (not folder or path.startswith(folder + "/"))], folder)
    found = _undeclared(scope, roots, local)
    order = {name: place for place, name in enumerate(roots)}
    return [{**_undeclared_item(scope, name, listed, []), "line": lines[name], "manifests": [m["path"] for m in scope["manifests"]]}
            for name, listed in sorted(found, key=lambda item: order[item[0]])]


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The dependencies the manifests declare that no code imports and the imports no manifest declares.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) entries listed per list.
    Returns: dict {"manifests": [read paths], "manifests_skipped": [{"path", "reason"}], "unused": [{"manifest", "ecosystem",
    "name", "specifier", "confidence", "reason", ["alias_candidates"]}], "undeclared": [{"folder", "language", "name",
    "files", "count", "confidence", "reason", ["alias_candidates"]}], "counts": {"manifests", "declared", "imported",
    "unused", "undeclared"}, "notes": [str], "note": str}. `confidence` is `medium` or `low`, never certain: see NOTE.
    A folder with no code file of the manifest's language in the graph is skipped with its reason, never reported as unused.
    Raises: ValueError when no manifest of a supported kind exists; ValueError or RuntimeError when the code graph cannot be
    built.
    """
    manifests, skipped, skipped_keys = [], [], []
    for rel in dependency_notice.manifests(root):
        found, reason = _manifest(root, rel)
        if found:
            manifests.append(found)
        else:
            skipped.append({"path": rel, "reason": reason})
            key = dependency_notice.ecosystem_of(rel)
            if key in LANGUAGES:
                skipped_keys.append((LANGUAGES[key], posixpath.dirname(rel)))
    if not manifests:
        why = "; ".join(f"{item['path']}: {item['reason']}" for item in skipped) or "none was found"
        raise ValueError(f"No manifest to analyze (package.json, requirements*.txt, pyproject.toml, go.mod): {why}.")
    scopes = _scopes(manifests, skipped_keys)
    folders = defaultdict(set)
    for language, folder in scopes:
        folders[language].add(folder)
    data = code_graph.build_current(root, view_id, shared=True)
    files, roots = defaultdict(list), defaultdict(lambda: defaultdict(set))
    for item in data.get("files") or []:
        language = _EXTENSIONS.get(posixpath.splitext(item["path"])[1])
        owner = _owner(item["path"], folders[language]) if language else None
        if owner is not None:
            files[(language, owner)].append(item["path"])
    for dep in data.get("dependencies") or []:
        language = _EXTENSIONS.get(posixpath.splitext(dep["source"])[1])
        owner = _owner(dep["source"], folders[language]) if language else None
        name = _root(dep["specifier"], language) if owner is not None and dep.get("resolution") == "external" else None
        if name:
            roots[(language, owner)][name].add(dep["source"])
    cut, unused, undeclared, declared, imported = bool(data.get("truncated")), [], [], 0, 0
    for key, scope in scopes.items():
        if not files[key]:
            for manifest in scope["manifests"]:
                skipped.append({"path": manifest["path"], "reason": f"no {scope['language']} file below "
                                f"{scope['folder'] or '.'} was read by the code graph, so nothing can be said"})
            continue
        found = _undeclared(scope, roots[key], _local_names(files[key], scope["folder"]))
        gone = _unused(scope, roots[key], [name for name, _ in found], cut)
        unused += gone
        free = [item["name"] for item in gone if not item["tool"]]
        undeclared += [_undeclared_item(scope, name, listed, free) for name, listed in found]
        declared += sum(len(m["pairs"]) for m in scope["manifests"])
        imported += len(roots[key])
    unused.sort(key=lambda item: (item["manifest"], item["name"]))
    undeclared.sort(key=lambda item: (item["folder"], -item["count"], item["name"]))
    notes = [code_graph.left_out_sentence(data)] if cut else []
    return {"manifests": [m["path"] for m in manifests], "manifests_skipped": skipped,
            "unused": [{k: v for k, v in item.items() if k != "tool"} for item in unused[:limit]], "undeclared": undeclared[:limit],
            "counts": {"manifests": len(manifests), "declared": declared, "imported": imported, "unused": len(unused),
                       "undeclared": len(undeclared)}, "notes": notes, "note": NOTE}
