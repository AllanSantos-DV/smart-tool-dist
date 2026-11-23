"""Source files nothing reaches, the file-level companion of `dead_code`: Python and JavaScript/TypeScript files that no
code imports, no test imports or names in a string, and no README, `docs` file, config or script names, and that are not an
entry point.

The imports are the resolved edges of the code graph (the importer listing of `delete_preview` and `move_preview`), the
strings that name a file are read the way `affected_tests` reads them, and the docs are the README and `docs` files that
`doc_refs` reads. Nothing is written.
"""
import json
import os
import posixpath
import re

import affected_tests
import code_graph
import index_profile
import index_scope
import project_scan
import rename_preview

DEFAULT_LIMIT = 30
PYTHON = (".py",)
SCRIPT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
CONFIG_EXTENSIONS = (".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".conf", ".mk", ".sh", ".ps1", ".bat", ".cmd",
                     ".gradle", ".xml")
CONFIG_NAMES = ("makefile", "dockerfile", "procfile", "justfile")
MARKERS = ("__init__.py", "__main__.py")
HOOK_FOLDERS = ("hooks", ".husky", ".githooks")
NOTE = ("Candidates from the static import graph, Python and JavaScript/TypeScript files only (Go and Java files see their "
        "package siblings without an import): read each one before deleting it. A file counts as reached when a file of the "
        "graph imports it, a test imports it or writes its path in a string, or a README, `docs` file, config or script "
        "writes its path or file name. Files loaded by config, hooks, console scripts, a package main, a plugin loader or a "
        "name built at run time may be false positives; package.json entries, `__main__`, `__init__`, hook files and files "
        "that run themselves are left out as entry points.")

_TOKEN = re.compile(r"[\w.@+/-]+")
_MODULE_REF = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*):[A-Za-z_]|(?:^|\s)-m\s+([A-Za-z_][\w.]*)")
_SELF_RUN = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]|require\.main\s*===?\s*module|import\.meta\.main", re.M)


def _is_config(rel):
    """Whether a file is a config or script a project writes file names in (a manifest, a workflow, a launcher)."""
    name = posixpath.basename(rel).lower()
    return name.endswith(CONFIG_EXTENSIONS) or name in CONFIG_NAMES or name.startswith("dockerfile")


def _read(root, rel):
    """The text of a project file; undecodable bytes are replaced, an unreadable file reads as empty."""
    try:
        with open(os.path.join(root, *rel.split("/")), "rb") as stream:
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _manifest_entries(folder, text):
    """The files a package.json declares as its entry points, relative to the project, or None when it is not valid JSON.

    The entry of `main`, `module`, `browser`, `bin` and `exports` is taken as written, with a script extension or an
    `index` file when it names a folder or omits the extension; a manifest without `main` enters through `index.js`.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    values = []

    def collect(node):
        if isinstance(node, str):
            values.append(node)
        elif isinstance(node, dict):
            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    if isinstance(data, dict):
        for key in ("main", "module", "browser", "bin", "exports"):
            collect(data.get(key))
        if not data.get("main"):
            values.append("index.js")
    found = set()
    for value in values:
        base = posixpath.normpath(posixpath.join(folder, value.replace("\\", "/")))
        found.add(base)
        found.update(base + ext for ext in SCRIPT)
        found.update(f"{base}/index{ext}" for ext in SCRIPT)
    return found


def _names(texts):
    """What the docs and configs write, as (names, modules): `names` holds every path-like or dotted word, as written and
    joined to the folder of its file, plus the dotted prefixes of a dotted word; `modules` holds the Python modules written
    as `module:attribute` or after `-m`, where a single word is a module and not prose."""
    names, modules = set(), set()
    for folder, text, config in texts:
        for first, second in _MODULE_REF.findall(text):
            modules.add(first or second)
        for raw in _TOKEN.findall(text.replace("\\", "/") if config else text):
            token = raw.rstrip(".,;:")
            while token.startswith("./"):
                token = token[2:]
            if "." not in token and "/" not in token:
                continue
            names.add(token)
            if folder:
                names.add(posixpath.normpath(posixpath.join(folder, token)))
            if "/" not in token:
                parts = token.split(".")
                names.update(".".join(parts[:count]) for count in range(2, len(parts)))
    return names, modules


def _module_forms(rel):
    """The dotted names a Python file can be imported or run as, longest first: `src/pkg/cli.py` is `src.pkg.cli`,
    `pkg.cli` and `cli`."""
    parts = rel[:-len(".py")].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return [".".join(parts[start:]) for start in range(len(parts))]


def _written(rel, names, modules):
    """Whether a doc or config writes the path of the file, its bare file name, or its Python module."""
    if rel in names or posixpath.basename(rel) in names:
        return True
    if rel.endswith(PYTHON):
        return any(form in modules or "." in form and form in names for form in _module_forms(rel))
    return False


def _entry_point(rel, text, declared):
    """Whether a file is entered from outside the import graph: a package marker or `__main__`, a declared package.json
    entry, a file of a hooks folder, a script with a shebang or a file that runs itself."""
    return (posixpath.basename(rel) in MARKERS or rel in declared
            or any(part in HOOK_FOLDERS for part in rel.split("/")[:-1])
            or text.startswith("#!") or bool(_SELF_RUN.search(text)))


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The source files nothing imports, names or runs.

    Parameters: root (str) project folder, a git repository; view_id (str or None) index view; limit (int) files returned.
    Returns: dict {"orphans": [{"path", "lines"}] in path order, "counts": {"orphans", "candidates", "entry_points"
    (the files nothing else reaches that were left out as entry points)}, "manifest_note": str (the package.json files that
    are not valid JSON, whose entries are unknown; "" when all read), "graph_note": str (what the analysis limits cut, "" when nothing), "note": str}.
    Raises: ValueError when the code graph is empty or git cannot list the folder.
    """
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("files"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    known = {item["path"] for item in data["files"]}
    kinds = {path: index_profile.kind(path, profile) for path in known}
    reached = {dep["target"] for dep in data.get("dependencies") or [] if dep.get("target") and dep["target"] != dep["source"]}
    sources = {path for path, kind in kinds.items() if kind in ("code", "test")}
    for found in (affected_tests._path_literal_users(root, sources, known),
                  affected_tests._import_string_users(root, sources, known)):
        reached.update(target for target, users in found.items() if users)
    texts, declared, broken = [], set(), []
    for rel in project_scan.tracked_files(root):
        doc = rename_preview._is_doc(rel)
        if not doc and not (_is_config(rel) and index_profile.kind(rel, profile) == "code"):
            continue
        text = project_scan._text(os.path.join(root, rel))
        if text is None:
            continue
        folder = posixpath.dirname(rel)
        texts.append((folder, text, not doc))
        if posixpath.basename(rel) == "package.json":
            entries = _manifest_entries(folder, text)
            if entries is None:
                broken.append(rel)
            else:
                declared |= entries
    names, modules = _names(texts)
    parsed = {item["path"] for item in data["files"] if item.get("analysis") == "parsed"}
    candidates = sorted(path for path in parsed if kinds[path] == "code" and path.lower().endswith(PYTHON + SCRIPT))
    orphans, entries = [], 0
    for rel in candidates:
        if rel in reached or _written(rel, names, modules):
            continue
        text = _read(root, rel)
        if _entry_point(rel, text, declared):
            entries += 1
        else:
            orphans.append({"path": rel, "lines": len(text.splitlines())})
    manifest_note = (f"{', '.join(broken)} could not be read as JSON, so the entry points they declare are unknown."
                     if broken else "")
    return {"orphans": orphans[:limit],
            "counts": {"orphans": len(orphans), "candidates": len(candidates), "entry_points": entries},
            "manifest_note": manifest_note,
            "graph_note": code_graph.left_out_sentence(data) if data.get("truncated") else "", "note": NOTE}
