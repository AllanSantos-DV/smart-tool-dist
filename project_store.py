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
MAX_JOBS = 500


@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT NOT NULL, data TEXT NOT NULL, updated_at REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT, status TEXT NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id,updated_at)")
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
            "status": "registered", "watch": False, "paused": False, "enabled": False,
            "update_mode": "on_search",
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
        merged = set(project.get('dirty_paths', [])) | set(paths)
        full = full or project.get('needs_full_check', False) or len(merged) > 1000
        update(project_id, dirty_paths=[] if full else sorted(merged), needs_full_check=full,
               scope_dirty=scope or project.get('scope_dirty', False))


def confirm_changes(project_id, sequence):
    with _LOCK:
        project = get(project_id)
        if project and project.get('dirty_seq', 0) == sequence:
            update(project_id, dirty_paths=[], needs_full_check=False, scope_dirty=False)


def save_job(job_id, payload):
    text = json.dumps(payload, ensure_ascii=False)
    protected = secret_store.protect(text)
    if not secret_store.is_protected(protected):
        raise RuntimeError("Could not protect the job history on this Windows system.")
    with _LOCK, _db() as db:
        db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)",
                   (job_id, payload.get("project_id"), payload.get("status", "pending"), time.time(), protected))
        db.execute("DELETE FROM jobs WHERE status!='pending' AND id NOT IN "
                   "(SELECT id FROM jobs ORDER BY updated_at DESC LIMIT ?)", (MAX_JOBS,))


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
