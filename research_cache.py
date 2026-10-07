#!/usr/bin/env python3
"""Pesquisas reutilizáveis com conteúdo protegido por DPAPI no perfil Windows.

O SQLite guarda apenas hash da consulta, horários e um blob cifrado. Consultas,
trechos, achados e URLs ficam dentro do blob. Falha de leitura/gravação desliga o
reúso nesta chamada; a pesquisa web continua normalmente.
"""
import array
import base64
import hashlib
import json
import os
import paths
import sqlite3
import threading
import time
from contextlib import contextmanager

import secret_store


CACHE_PATH = os.path.join(paths.DATA_DIR, "research-cache.sqlite3")
MAX_ENTRIES = 500
# Namespaces with their own quota stay out of MAX_ENTRIES, so web_fetch pages never evict web_search answers.
OWN_QUOTA = {"web_fetch": 300}
SCHEMA = 2
MAX_AGE_S = 30 * 86400
_LOCK = threading.RLock()


def _key(depth, query):
    normalized = " ".join(str(query or "").casefold().split())
    return hashlib.sha256(f"{depth}\0{normalized}".encode("utf-8")).hexdigest()


def _connect():
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    connection = sqlite3.connect(CACHE_PATH, timeout=5)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            depth TEXT NOT NULL,
            payload TEXT NOT NULL,
            saved_at REAL NOT NULL,
            validated_at REAL NOT NULL
        )
    """)
    return connection


@contextmanager
def _connection():
    db = _connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def candidates(depth, query, limit=200):
    """Entrada exata primeiro, depois recentes para confirmação semântica."""
    if not secret_store.available():
        return []
    key = _key(depth, query)
    try:
        with _LOCK, _connection() as db:
            db.execute("DELETE FROM entries WHERE saved_at < ?", (time.time() - MAX_AGE_S,))
            rows = db.execute(
                "SELECT id, payload, saved_at, validated_at FROM entries "
                "WHERE depth = ? ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, "
                "validated_at DESC LIMIT ?", (depth, key, limit),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    result = []
    for entry_id, protected, saved_at, validated_at in rows:
        try:
            if not secret_store.is_protected(protected):
                continue
            payload = json.loads(secret_store.unprotect(protected))
            if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
                continue
            vector = payload.pop("topic_vector", None)
            if isinstance(vector, dict):
                values = array.array("f")
                values.frombytes(base64.b64decode(vector["b64"]))
                payload["topic_vector"] = {"model": vector["model"], "values": values}
            result.append({"id": entry_id, "saved_at": saved_at,
                           "validated_at": validated_at, **payload})
        except (ValueError, TypeError, KeyError, secret_store.SecretProtectionError):
            continue
    return result


def _packed_vector(model, vector):
    if not model or not vector:
        return None
    return {"model": model, "b64": base64.b64encode(array.array("f", vector).tobytes()).decode("ascii")}


def put(depth, query, result, classification, topic_model=None, topic_vector=None, origin=None):
    """Grava a pesquisa e devolve o horário da gravação, ou None se não gravou."""
    if not secret_store.available():
        return None
    now = time.time()
    saved_at, validated_at = (origin["saved_at"], origin["validated_at"]) if origin else (now, now)
    payload = {"schema": SCHEMA, "query": query, "depth": depth,
               "result": result, "classification": classification}
    if origin:
        payload["origin_query"] = origin.get("origin_query") or origin["query"]
    packed = _packed_vector(topic_model, topic_vector)
    if packed:
        payload["topic_vector"] = packed
    try:
        protected = secret_store.protect(json.dumps(payload, ensure_ascii=False))
        with _LOCK, _connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO entries(id, depth, payload, saved_at, validated_at) "
                "VALUES (?, ?, ?, ?, ?)", (_key(depth, query), depth, protected, saved_at, validated_at),
            )
            own = sorted(OWN_QUOTA)
            marks = ",".join("?" * len(own))
            if depth in OWN_QUOTA:
                db.execute("DELETE FROM entries WHERE depth = ? AND id NOT IN "
                           "(SELECT id FROM entries WHERE depth = ? ORDER BY saved_at DESC LIMIT ?)",
                           (depth, depth, OWN_QUOTA[depth]))
            else:
                db.execute(f"DELETE FROM entries WHERE depth NOT IN ({marks}) AND id NOT IN "
                           f"(SELECT id FROM entries WHERE depth NOT IN ({marks}) ORDER BY saved_at DESC LIMIT ?)",
                           (*own, *own, MAX_ENTRIES))
        return saved_at
    except (OSError, ValueError, TypeError, sqlite3.Error, secret_store.SecretProtectionError):
        return None


def complete(depth, query, saved_at, classification, topic_model=None, topic_vector=None):
    """Completa a entrada gravada em saved_at; uma regravação posterior não é sobrescrita."""
    try:
        with _LOCK, _connection() as db:
            row = db.execute("SELECT payload FROM entries WHERE id = ? AND saved_at = ?",
                             (_key(depth, query), saved_at)).fetchone()
            if row is None:
                return False
            payload = json.loads(secret_store.unprotect(row[0]))
            payload["classification"] = classification
            packed = _packed_vector(topic_model, topic_vector)
            if packed:
                payload["topic_vector"] = packed
            db.execute("UPDATE entries SET payload = ? WHERE id = ? AND saved_at = ?",
                       (secret_store.protect(json.dumps(payload, ensure_ascii=False)),
                        _key(depth, query), saved_at))
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error, secret_store.SecretProtectionError):
        return False


def mark_validated(entry_id):
    try:
        with _LOCK, _connection() as db:
            db.execute("UPDATE entries SET validated_at = ? WHERE id = ?", (time.time(), entry_id))
    except (OSError, sqlite3.Error):
        pass


def discard(entry_id):
    try:
        with _LOCK, _connection() as db:
            db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    except (OSError, sqlite3.Error):
        pass
