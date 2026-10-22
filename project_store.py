"""Cadastro de projetos e histórico de jobs. Consultas/resultados ficam sob DPAPI."""
import json
import os
import paths
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

import project_identity
import secret_store

DB_PATH = os.path.join(paths.DATA_DIR, "projects.sqlite3")
_LOCK = threading.RLock()
_SCHEMA_READY = set()
MAX_JOBS = 500


@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not os.path.isfile(DB_PATH):
        _SCHEMA_READY.discard(DB_PATH)
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        db.execute("PRAGMA secure_delete=ON")
        if DB_PATH not in _SCHEMA_READY:
            db.execute("CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT NOT NULL, data TEXT NOT NULL, updated_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT, status TEXT NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id,updated_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,project_id)")
            _SCHEMA_READY.add(DB_PATH)
        with db:
            yield db
    finally:
        db.close()


def register(root, watch=None):
    display = project_identity.display_root(root)
    if not os.path.isdir(display):
        raise ValueError("The project folder does not exist or is not accessible.")
    key = project_identity.project_id(display)
    with _LOCK, _db() as db:
        row = db.execute("SELECT data FROM projects WHERE id=?", (key,)).fetchone()
        data = json.loads(row[0]) if row else {
            "id": key, "root": display, "name": os.path.basename(display) or display,
            "status": "registered", "watch": True, "paused": False, "enabled": False,
            "update_mode": "eager",
            "dirty_seq": 0, "indexed_seq": 0, "created_at": time.time(),
            "last_indexed": None, "last_checked": None, "last_error": "",
            "last_job_id": None, "watch_backend": "off", "next_retry_at": 0,
        }
        data["root"] = display
        if watch is not None:
            data["watch"] = bool(watch)
        db.execute("INSERT OR REPLACE INTO projects VALUES(?,?,?,?)",
                   (key, display, json.dumps(data, ensure_ascii=False), time.time()))
    return data


def get(project_id):
    with _LOCK, _db() as db:
        row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
    return json.loads(row[0]) if row else None


def containing(path):
    """The registered project whose folder holds `path` (the deepest one when projects nest), or `None`."""
    target = project_identity.canonical_root(path)
    inside = [p for p in all_projects() if target.startswith(project_identity.canonical_root(p["root"]) + os.sep)]
    return max(inside, key=lambda p: len(p["root"])) if inside else None


def all_projects():
    with _LOCK, _db() as db:
        rows = db.execute("SELECT data FROM projects ORDER BY root COLLATE NOCASE").fetchall()
    return [json.loads(row[0]) for row in rows]


def update(project_id, **fields):
    with _LOCK, _db() as db:
        row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        fields.pop("id", None)
        fields.pop("root", None)
        data.update(fields)
        db.execute("UPDATE projects SET data=?,updated_at=? WHERE id=?",
                   (json.dumps(data, ensure_ascii=False), time.time(), project_id))
    return data


def mark_dirty(project_id, reason="Arquivos alterados."):
    with _LOCK:
        project = get(project_id)
        if not project:
            return None
        return update(project_id, dirty_seq=project.get("dirty_seq", 0) + 1,
                      status="dirty" if not project.get("paused") else "paused",
                      dirty_reason=reason, last_event=time.time())


def record_changes(project_id, paths=(), full=False, scope=False):
    with _LOCK:
        project = get(project_id)
        if not project:
            return
        update(project_id, **_changes_fields(project, paths, full, scope))


def _changes_fields(project, paths, full, scope):
    """`needs_full_check` / `scope_dirty` remember the `dirty_seq` at which they were raised (`*_seq`), so a job that ran
    after that sequence can clear them even while events keep arriving."""
    merged = set(project.get('dirty_paths', [])) | set(paths)
    full = full or project.get('needs_full_check', False)
    scope = scope or project.get('scope_dirty', False)
    seq = project.get('dirty_seq', 0)
    # Past 1000 paths the list stops helping: the update compares stat of the whole scope instead.
    return {"dirty_paths": [] if len(merged) > 1000 else sorted(merged), "needs_full_check": full,
            "scope_dirty": scope,
            "full_check_seq": seq if full and not project.get('needs_full_check') else project.get('full_check_seq', 0),
            "scope_dirty_seq": seq if scope and not project.get('scope_dirty') else project.get('scope_dirty_seq', 0)}


def note_changes(project_id, reason, paths=(), full=False, scope=False):
    """mark_dirty + record_changes in one read and one write of the registry; a burst of file events costs one commit per
    batch instead of two reads and two commits."""
    with _LOCK, _db() as db:
        row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["dirty_seq"] = data.get("dirty_seq", 0) + 1
        data.update(status="dirty" if not data.get("paused") else "paused", dirty_reason=reason,
                    last_event=time.time(), **_changes_fields(data, paths, full, scope))
        db.execute("UPDATE projects SET data=?,updated_at=? WHERE id=?",
                   (json.dumps(data, ensure_ascii=False), time.time(), project_id))
    return data


def confirm_changes(project_id, sequence):
    """Clears what the finished job covered: the path list only if nothing changed since the job captured `sequence`;
    the full-check and scope flags if they were raised at or before it (the job did both from that state)."""
    with _LOCK:
        project = get(project_id)
        if not project:
            return
        fields = {}
        if project.get('dirty_seq', 0) == sequence:
            fields.update(dirty_paths=[], needs_full_check=False, scope_dirty=False)
        else:
            if project.get('needs_full_check') and project.get('full_check_seq', 0) <= sequence:
                fields["needs_full_check"] = False
            if project.get('scope_dirty') and project.get('scope_dirty_seq', 0) <= sequence:
                fields["scope_dirty"] = False
        if fields:
            update(project_id, **fields)


def projects_with_pending_job():
    """Ids of the projects with a job still pending, from the `status` column: no job payload is decrypted."""
    with _LOCK, _db() as db:
        return {row[0] for row in db.execute("SELECT DISTINCT project_id FROM jobs WHERE status='pending'")}


def save_job(job_id, payload):
    text = json.dumps(payload, ensure_ascii=False)
    protected = secret_store.protect(text)
    if not secret_store.is_protected(protected):
        raise RuntimeError("Could not protect the job history on this Windows system.")
    with _LOCK, _db() as db:
        db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)",
                   (job_id, payload.get("project_id"), payload.get("status", "pending"), time.time(), protected))
        # The threshold comes from the updated_at index alone: ordering the payload rows read the whole jobs table
        # (1.7 MB) on every save, several saves per job.
        db.execute("DELETE FROM jobs WHERE status!='pending' AND updated_at < "
                   "(SELECT updated_at FROM jobs ORDER BY updated_at DESC LIMIT 1 OFFSET ?)", (MAX_JOBS - 1,))


def _decode(payload):
    try:
        if not secret_store.is_protected(payload):
            return None
        value = json.loads(secret_store.unprotect(payload))
        return value if isinstance(value, dict) else None
    except (ValueError, secret_store.SecretProtectionError):
        return None


def job(job_id):
    with _LOCK, _db() as db:
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _decode(row[0]) if row else None


def jobs(project_id=None, limit=30):
    with _LOCK, _db() as db:
        if project_id:
            rows = db.execute("SELECT id,payload FROM jobs WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
                              (project_id, limit)).fetchall()
        else:
            rows = db.execute("SELECT id,payload FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"job_id": key, **data} for key, payload in rows if (data := _decode(payload))]


def recover_interrupted():
    with _LOCK, _db() as db:
        rows = db.execute("SELECT id,payload FROM jobs WHERE status='pending'").fetchall()
    for key, payload in rows:
        data = _decode(payload) or {}
        data.update(status="interrupted", phase="interrupted", finished_at=time.time(),
                    error="The daemon restarted. Resume the job; files already committed will be reused.")
        save_job(key, data)
        if data.get("project_id"):
            update(data["project_id"], status="interrupted", last_error=data["error"], last_job_id=key)
    return len(rows)


def forget(project_id):
    with _LOCK, _db() as db:
        db.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        for table in ('usage_daily','usage_index_jobs','usage_imports'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
                db.execute(f'DELETE FROM {table} WHERE project_id=?',(project_id,))


def relocate(project_id, root):
    """Reassocia cadastro e histórico após a cópia do índice; não move código-fonte."""
    display = project_identity.display_root(root)
    key = project_identity.project_id(display)
    with _LOCK, _db() as db:
        if db.execute("SELECT 1 FROM projects WHERE id=?", (key,)).fetchone():
            raise ValueError("The target folder is already registered.")
        row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ValueError("Project not registered.")
        data = json.loads(row[0])
        data.update(id=key, root=display, name=os.path.basename(display) or display,
                    previous_root=data["root"], status="dirty", needs_full_check=True,
                    dirty_seq=data.get("dirty_seq", 0) + 1, watch_backend="off", last_error="")
        db.execute("INSERT INTO projects VALUES(?,?,?,?)", (key, display, json.dumps(data, ensure_ascii=False), time.time()))
        for job_id, payload in db.execute("SELECT id,payload FROM jobs WHERE project_id=?", (project_id,)).fetchall():
            job_data = _decode(payload)
            if job_data:
                job_data["project_id"] = key
                job_data.setdefault("original_project_root", data["previous_root"])
                if job_data.get("arguments"):
                    job_data["arguments"]["project_root"] = display
                payload = secret_store.protect(json.dumps(job_data, ensure_ascii=False))
            db.execute("UPDATE jobs SET project_id=?,payload=? WHERE id=?", (key, payload, job_id))
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        for table in ('usage_daily','usage_index_jobs','usage_imports'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
                db.execute(f'UPDATE {table} SET project_id=? WHERE project_id=?',(key,project_id))
    return data


def import_known_legacy(index_dir):
    """Somente raízes documentadas nos registros antigos; não adivinha diretórios."""
    import indexer
    candidates = set()
    for name in ("daemon.log", "daemon.log.1"):
        try:
            with open(os.path.join(index_dir, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    match = re.search(r"reindex (.+?): \d+ no escopo", line)
                    if match and os.path.isdir(match.group(1)):
                        candidates.add(match.group(1))
        except OSError:
            pass
    for root in candidates:
        key = project_identity.project_id(root)
        if get(key) or not indexer.existing_db_path(root):
            continue
        register(root, watch=False)
        update(key, enabled=True, status="migration_required", legacy_discovered=True)
