"""Removes what Smart Tool left on disk that no registered project needs.

Runs once when the daemon starts, before the project monitor: the registry of projects (a folder that is not a
project, see project_admission.junk_reason, is never one of them, and each removal is listed in a manifest), write temporaries orphaned by a crash, generations under construction that stopped advancing,
the `.previous` copies older versions kept of a replaced generation, index files of projects no longer registered,
and saved code-graph analyses of index views that no longer exist. Source files are never touched."""
import json
import os
import re
import time

import atomic_io
import indexer
import paths
import project_admission
import project_identity
import project_store

MANIFEST_DIR = os.path.join(paths.DATA_DIR, "junk-manifests")

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


def junk_projects(errors=None):
    """The registered projects that were never projects, as [{id, root, name, reason}], read-only: nothing is
    removed. A project whose folder cannot be listed is left out and, when `errors` is a list, reported in it."""
    found = []
    for project in project_store.all_projects():
        try:
            reason = project_admission.junk_reason(project)
        except OSError as exc:
            if errors is not None:
                errors.append(f"{project['root']}: {exc}")
            continue
        if reason:
            found.append({"id": project["id"], "root": project["root"], "name": project.get("name"), "reason": reason})
    return found


def remove_junk(entries, log=None):
    """Unregisters the given junk projects and deletes their index files (never the project's own files), then writes
    a manifest of what was removed and what was kept; returns {removed, kept, files, bytes, manifest}. A project whose
    index another process holds open is kept and listed with the reason."""
    log = log or (lambda message: None)
    report = {"removed": [], "kept": [], "files": 0, "bytes": 0, "manifest": None}
    for entry in entries:
        files = {"files": [], "bytes": 0}
        try:
            remove_project_files(entry["root"], files)
        except (RuntimeError, OSError) as exc:
            report["kept"].append({**entry, "error": str(exc)})
            log(f"disk cleanup: {entry['root']} kept: {exc}")
            continue
        project_store.forget(entry["id"])
        report["removed"].append({**entry, "index_files": files["files"], "bytes": files["bytes"]})
        report["files"] += len(files["files"])
        report["bytes"] += files["bytes"]
        log(f"disk cleanup: unregistered {entry['root']}: {entry['reason']}")
    if report["removed"] or report["kept"]:
        os.makedirs(MANIFEST_DIR, exist_ok=True)
        report["manifest"] = os.path.join(MANIFEST_DIR, time.strftime("junk-%Y%m%d-%H%M%S.json"))
        atomic_io.write_text(report["manifest"], json.dumps(
            {"removed": report["removed"], "kept": report["kept"], "source_files_touched": False}, indent=1))
    return report


def sweep(extra_roots, log=None):
    """Removes the waste described in the module docstring and returns {junk, files, bytes, errors}.

    extra_roots are folders that own a vector cache without being projects (the web_fetch cache). A file that
    cannot be removed, or a registered folder that cannot be listed, is reported in errors and logged, and the sweep
    goes on with the rest: the daemon starts its project monitor right after, so the sweep must not stop it."""
    log = log or (lambda message: None)
    report = {"junk": [], "files": [], "bytes": 0, "errors": []}
    removal = remove_junk(junk_projects(report["errors"]), log)
    report["bytes"] += removal["bytes"]
    report["junk"] = [{"root": e["root"], "reason": e["reason"]} for e in removal["removed"]]
    report["errors"] += [f"{e['root']}: {e['error']}" for e in removal["kept"]]
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
