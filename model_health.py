"""Failure log and failure rate of the gateway models, per model and role (router, scope, scout, research, embedding,
rerank). Never stores request content: the model, the role, the tool that was running, the HTTP path, a short error
text and the time.

Three consumers read it: the tool answer (a warning line when a call of that very tool failed), the setup screen
(rate table and recent failures) and the tray (one toast per model per day when the rate passes the limit).
"""
import contextlib
import contextvars
import io
import os
import sqlite3
import time

import config
import project_store

WINDOW_S = 24 * 3600
ALERT_MIN_CALLS = 5
ALERT_RATE = 0.5
ALERT_EVERY_S = 24 * 3600
MUTE_S = 7 * 24 * 3600
KEEP_S = 7 * 24 * 3600
KEEP_FAILURES = 500
ERROR_CHARS = 300
_ROLES = ("router_model", "scope_model", "scout_model", "research_model", "embedding_model", "rerank_model")
_PATHS = ("/v1/chat/completions", "/v1/embeddings", "/v1/rerank")
_TOOL = contextvars.ContextVar("model_health_tool", default=None)
_COLLECTED = contextvars.ContextVar("model_health_collected", default=None)
_SCHEMA = set()


@contextlib.contextmanager
def tool_call(name):
    """Marks the model calls made inside the block as run by tool `name` and collects their failures; the block yields
    the list, which `warning_text` turns into the line appended to the tool answer."""
    failures = []
    tool_token, list_token = _TOOL.set(name), _COLLECTED.set(failures)
    try:
        yield failures
    finally:
        _TOOL.reset(tool_token)
        _COLLECTED.reset(list_token)


def _db():
    return project_store._db()


def _tables(db):
    if project_store.DB_PATH in _SCHEMA and os.path.isfile(project_store.DB_PATH):
        return
    db.execute("CREATE TABLE IF NOT EXISTS model_calls(hour INTEGER,model TEXT,role TEXT,calls INTEGER,errors INTEGER,"
               "PRIMARY KEY(hour,model,role))")
    db.execute("CREATE TABLE IF NOT EXISTS model_failures(at REAL,model TEXT,role TEXT,tool TEXT,path TEXT,error TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS model_alerts(model TEXT PRIMARY KEY,last_notified REAL,muted_until REAL)")
    _SCHEMA.add(project_store.DB_PATH)


def role_of(model, cfg=None):
    """Name of the config field that selects `model` ('other' when none does)."""
    cfg = cfg if cfg is not None else config.load_config()
    return next((field for field in _ROLES if cfg.get(field) == model), "other")


def error_text(exc):
    """Short text of a failed call. An HTTP error body is read once and put back so the caller can still read it."""
    text = f"{type(exc).__name__}: {exc}"
    fp = getattr(exc, "fp", None)
    if fp is not None and hasattr(exc, "read"):
        try:
            detail = exc.read()
            exc.read = io.BytesIO(detail).read
            text += " | " + detail.decode("utf-8", "replace")
        except Exception:
            pass
    return " ".join(text.split())[:ERROR_CHARS]


def record(path, body, failure=None):
    """Counts one model call and, when it failed, logs it. Never raises: the log must not change the call's result."""
    if path not in _PATHS:
        return
    try:
        model = str((body or {}).get("model", "")) if isinstance(body, dict) else ""
        role = role_of(model)
        now = time.time()
        text = error_text(failure) if failure is not None else None
        with project_store._LOCK, _db() as db:
            _tables(db)
            db.execute("INSERT INTO model_calls VALUES(?,?,?,1,?) ON CONFLICT(hour,model,role) DO UPDATE SET "
                       "calls=calls+1,errors=errors+excluded.errors", (int(now // 3600), model, role, int(text is not None)))
            if text is not None:
                db.execute("INSERT INTO model_failures VALUES(?,?,?,?,?,?)", (now, model, role, _TOOL.get() or "", path, text))
                db.execute("DELETE FROM model_failures WHERE at<? OR rowid NOT IN "
                           "(SELECT rowid FROM model_failures ORDER BY at DESC LIMIT ?)", (now - KEEP_S, KEEP_FAILURES))
                db.execute("DELETE FROM model_calls WHERE hour<?", (int((now - KEEP_S) // 3600),))
        collected = _COLLECTED.get()
        if text is not None and collected is not None:
            collected.append({"model": model, "role": role, "error": text})
    except Exception:
        pass


def warning_text(failures, setup_url):
    """The line a tool answer carries when a model call inside it failed ('' when none did)."""
    if not failures:
        return ""
    first = failures[0]
    more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
    return (f"\n\nSmart Tool model warning: {first['role']} `{first['model']}` failed in this call{more}: "
            f"{first['error']}. Pick another model: {setup_url}/setup#models")


def summary(now=None):
    """Per model and role over the last 24 h, plus the 20 latest failures."""
    now = now or time.time()
    with project_store._LOCK, _db() as db:
        _tables(db)
        db.row_factory = sqlite3.Row
        models = [dict(row) for row in db.execute(
            "SELECT model,role,SUM(calls) AS calls,SUM(errors) AS errors FROM model_calls WHERE hour>=? "
            "GROUP BY model,role ORDER BY SUM(errors) DESC,SUM(calls) DESC", (int((now - WINDOW_S) // 3600),))]
        failures = [dict(row) for row in db.execute(
            "SELECT at,model,role,tool,path,error FROM model_failures WHERE at>=? ORDER BY at DESC LIMIT 20", (now - WINDOW_S,))]
        for row in models:
            last = db.execute("SELECT error FROM model_failures WHERE model=? AND role=? ORDER BY at DESC LIMIT 1",
                              (row["model"], row["role"])).fetchone()
            row["last_error"] = last[0] if last else ""
    for row in models:
        row["rate"] = round(row["errors"] / row["calls"], 3) if row["calls"] else 0.0
        row["failing"] = row["calls"] >= ALERT_MIN_CALLS and row["rate"] >= ALERT_RATE
    return {"window_hours": WINDOW_S // 3600, "models": models, "failures": failures}


def alerts_due(now=None):
    """Models over the failure limit that are neither muted nor already announced in the last 24 h; marks them
    announced, so a second call in the same day returns nothing."""
    now = now or time.time()
    due = []
    for row in summary(now)["models"]:
        if not row["failing"]:
            continue
        with project_store._LOCK, _db() as db:
            _tables(db)
            seen = db.execute("SELECT last_notified,muted_until FROM model_alerts WHERE model=?", (row["model"],)).fetchone()
            if seen and (seen[1] > now or now - seen[0] < ALERT_EVERY_S):
                continue
            db.execute("INSERT INTO model_alerts VALUES(?,?,0) ON CONFLICT(model) DO UPDATE SET last_notified=excluded.last_notified",
                       (row["model"], now))
        due.append(row)
    return due


def mute(model, now=None):
    """Silences the failure toast for `model` for 7 days (the failures stay in the log)."""
    if not model:
        raise ValueError("No model to mute.")
    now = now or time.time()
    with project_store._LOCK, _db() as db:
        _tables(db)
        db.execute("INSERT INTO model_alerts VALUES(?,?,?) ON CONFLICT(model) DO UPDATE SET muted_until=excluded.muted_until",
                   (model, now, now + MUTE_S))
    return f"failure alerts for {model} muted for 7 days"
