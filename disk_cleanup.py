"""Removes what Smart Tool left on disk that no registered project needs.

Runs once when the daemon starts, before the project monitor: the registry of projects (a folder of projects is
never one of them), write temporaries orphaned by a crash, generations under construction that stopped advancing,
the `.previous` copies older versions kept of a replaced generation, index files of projects no longer registered,
and saved code-graph analyses of index views that no longer exist. Source files are never touched."""
import os
import re
import time

import atomic_io
import indexer
import project_identity
import project_store

# A generation under construction resumes where it stopped; one untouched this long belongs to a model or scope that
# is no longer used, and a new one reuses the cached vectors anyway.
STALE_BUILD_S = 7 * 86400
_STATE_FILE = re.compile(r"([0-9a-f]{16})(?:\.v-[0-9a-f]{16})?(?:\.previous|\.building-[0-9a-f]+|\.vectors)?"
                         r"(?:\.sqlite3(?:-journal|-wal|-shm)?|\.scope\.json)")
_BUILDING = re.compile(r"[0-9a-f]{16}(?:\.v-[0-9a-f]{16})?\.building-[0-9a-f]+\.sqlite3")
_PREVIOUS = re.compile(r"[0-9a-f]{16}(?:\.v-[0-9a-f]{16})?\.previous\.sqlite3")
_TEMPORARY = re.compile(r".+" + re.escape(atomic_io.TMP_SUFFIX) + r"[0-9a-f]{32}")


def _remove(path, report):
    """Deletes one state file and adds its name and size to report."""
    size = os.path.getsize(path)
    os.remove(project_identity.state_path(path, indexer.INDEX_DIR))
    report["files"].append(os.path.basename(path))
    report["bytes"] += size


def remove_project_files(root, report=None):
    """Deletes every index, vector cache, scope and saved graph of the project at root (all Git views, the current and
    legacy ids); returns the report {files, bytes}. Raises RuntimeError when another process holds one open."""
    report = report if report is not None else {"files": [], "bytes": 0}
    keys = {project_identity.project_id(root), *project_identity.legacy_ids(root)}
    for name in sorted(os.listdir(indexer.INDEX_DIR)):
        match = _STATE_FILE.fullmatch(name)
        if not match or match.group(1) not in keys:
            continue
        path = os.path.join(indexer.INDEX_DIR, name)
        try:
            _remove(path, report)
        except PermissionError as exc:
            raise RuntimeError(f"{name} is open in another process. Close the external reader and try again.") from exc
        graph = indexer.graph_cache_path(path)
        if name.endswith(".sqlite3") and os.path.isfile(graph):
            report["bytes"] += os.path.getsize(graph)
            os.remove(graph)
    indexer.invalidate_cache(root)
    return report


def sweep(extra_roots, log=None):
    """Removes the waste described in the module docstring and returns {containers, files, bytes, errors}.

    extra_roots are folders that own a vector cache without being projects (the web_fetch cache). A file that
    cannot be removed, or a registered folder that cannot be listed, is reported in errors and logged, and the sweep
    goes on with the rest: the daemon starts its project monitor right after, so the sweep must not stop it."""
    log = log or (lambda message: None)
    report = {"containers": [], "files": [], "bytes": 0, "errors": []}
    for project in project_store.all_projects():
        if not os.path.isdir(project["root"]):
            continue
        try:
            refused = project_store.container_error(project["root"])
        except OSError as exc:
            report["errors"].append(f"{project['root']}: {exc}")
            log(f"disk cleanup: {project['root']} not checked as a folder of projects: {exc}")
            continue
        if refused:
            try:
                remove_project_files(project["root"], report)
            except RuntimeError as exc:
                report["errors"].append(str(exc))
                log(f"disk cleanup: {project['root']} kept: {exc}")
                continue
            project_store.forget(project["id"])
            report["containers"].append(project["root"])
            log(f"disk cleanup: unregistered {refused}")
    owners = [p["root"] for p in project_store.all_projects()] + list(extra_roots)
    keep = {key for root in owners for key in (project_identity.project_id(root), *project_identity.legacy_ids(root))}
    now = time.time()
    for name in sorted(os.listdir(indexer.INDEX_DIR)):
        path = os.path.join(indexer.INDEX_DIR, name)
        match = _STATE_FILE.fullmatch(name)
        try:
            if not os.path.isfile(path):
                continue
            age = now - os.path.getmtime(path)
            if _TEMPORARY.fullmatch(name):
                waste = age > atomic_io.ORPHAN_AGE_S
            elif match:
                waste = (match.group(1) not in keep or bool(_PREVIOUS.fullmatch(name)) or
                         bool(_BUILDING.fullmatch(name)) and age > STALE_BUILD_S)
            else:
                waste = False
            if waste:
                _remove(path, report)
        except OSError as exc:
            report["errors"].append(f"{name}: {exc}")
    graphs = os.path.join(indexer.INDEX_DIR, "graph-cache")
    if os.path.isdir(graphs):
        for name in sorted(os.listdir(graphs)):
            view = os.path.join(indexer.INDEX_DIR, name.removesuffix(".json.gz") + ".sqlite3")
            if name.endswith(".json.gz") and not os.path.isfile(view):
                path = os.path.join(graphs, name)
                try:
                    report["bytes"] += os.path.getsize(path)
                    os.remove(path)
                    report["files"].append("graph-cache/" + name)
                except OSError as exc:
                    report["errors"].append(f"graph-cache/{name}: {exc}")
    if report["files"] or report["errors"]:
        log(f"disk cleanup: removed {len(report['files'])} file(s), {report['bytes']} bytes; "
            f"{len(report['errors'])} error(s): {'; '.join(report['errors'][:5])}")
    return report
