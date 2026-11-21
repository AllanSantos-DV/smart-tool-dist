"""Per-function documentation registry: a crash-safe queue of the functions whose technical entry no longer matches
the code, with the dates of each change, kept in its own SQLite file (journal_mode=WAL, synchronous=FULL).

The stamps written in the pages (doc_scope.symbol_marker) stay the truth: the registry is a queue and a set of dates,
rebuilt from the pages and the disk when it is lost. A pending row and the change observed for it are written in the
same transaction (outbox), and processing is idempotent: a function whose page stamps already match the code is
settled without rewriting anything, so a crash between a page rename and the registry commit is repaired on resume.

Statuses: current, pending_code, pending_link, pending_prose, in_progress, orphan.
"""
import hashlib
import os
import sqlite3
import time

import paths
import project_identity

FILE = "doc_registry.db"
PENDING = ("pending_code", "pending_link", "pending_prose")
MAX_ATTEMPTS = 2
LEASE_S = 120


def database_path():
    """The registry file in the daemon's data folder."""
    return os.path.join(paths.DATA_DIR, FILE)


def connect():
    """Open the registry with WAL and synchronous=FULL, the setting SQLite documents as durable across power loss.

    Returns: sqlite3.Connection. Raises RuntimeError when SQLite refuses WAL, since the durability promise would not hold.
    """
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(database_path(), timeout=10)
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise RuntimeError(f"The documentation registry {database_path()} refused journal_mode=WAL (got {mode}).")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("CREATE TABLE IF NOT EXISTS projects (root TEXT PRIMARY KEY, suffix TEXT NOT NULL)")
    return conn


def _suffix(root):
    return hashlib.sha1(project_identity.canonical_root(root).encode("utf-8")).hexdigest()[:12]


def tables(conn, root):
    """Create the project's tables when missing and return their names: (functions, files, links)."""
    suffix = _suffix(root)
    names = (f"function_doc_{suffix}", f"file_state_{suffix}", f"feature_link_{suffix}")
    with conn:
        conn.execute("INSERT OR IGNORE INTO projects (root, suffix) VALUES (?, ?)", (root, suffix))
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {names[0]} (
            fn_id TEXT PRIMARY KEY, path TEXT NOT NULL, public INTEGER NOT NULL, has_docstring INTEGER NOT NULL,
            body_hash TEXT, docstring_hash TEXT, signature_hash TEXT,
            doc_body_hash TEXT, doc_docstring_hash TEXT, doc_signature_hash TEXT,
            method_updated_at REAL, doc_updated_at REAL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, lease_until REAL)""")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {names[1]} (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER)")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {names[2]} (
            feature_id TEXT NOT NULL, fn_id TEXT NOT NULL, layer TEXT, hops INTEGER, origin TEXT NOT NULL,
            score REAL, status TEXT NOT NULL, computed_at REAL NOT NULL, PRIMARY KEY (feature_id, fn_id))""")
    return names


def projects(conn):
    """Roots of the projects the registry tracks."""
    return [row[0] for row in conn.execute("SELECT root FROM projects ORDER BY root")]


def recover(conn, root, now=None):
    """Rows left in_progress by a stopped run whose lease expired go back to pending_code.

    Returns: int rows recovered.
    """
    functions = tables(conn, root)[0]
    with conn:
        cursor = conn.execute(f"UPDATE {functions} SET status='pending_code', lease_until=NULL "
                              "WHERE status='in_progress' AND (lease_until IS NULL OR lease_until < ?)",
                              (now or time.time(),))
    return cursor.rowcount


def changed_files(conn, root, candidates):
    """Candidate project files whose mtime_ns or size differ from the last state recorded (or that were never seen).

    Parameters: candidates (iterable of project-relative paths). Returns: dict path -> (mtime_ns, size), None for a
    file gone from disk.
    """
    files = tables(conn, root)[1]
    known = {path: (mtime, size) for path, mtime, size in conn.execute(f"SELECT path, mtime_ns, size FROM {files}")}
    found = {}
    for path in candidates:
        try:
            info = os.stat(os.path.join(root, path))
            state = (info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            state = None
        if known.get(path, ()) != state:
            found[path] = state
    return found


def observe(conn, root, functions_now, markers, file_states, gone=(), now=None):
    """Record what the code is now against what the pages say, and the file states read, in one transaction: a row whose
    code stamps differ from its page stamps becomes pending_code (its pending outbox entry), a matching one current.

    Parameters: functions_now (dict fn_id -> {path, public, has_docstring, signature, body, docstring}); markers (dict
    fn_id -> {signature, body, docstring} from the technical pages); file_states (dict path -> (mtime_ns, size) or
    None); gone (iterable of fn_id whose function no longer exists: orphan).
    Returns: list of fn_id now pending.
    """
    functions, files, _links = tables(conn, root)
    now = now or time.time()
    pending = []
    with conn:
        for fn_id, code in functions_now.items():
            page = markers.get(fn_id) or {}
            row = conn.execute(f"SELECT body_hash, status, attempts FROM {functions} WHERE fn_id=?", (fn_id,)).fetchone()
            matches = not code["public"] or all(page.get(part) == code[part] for part in ("signature", "body", "docstring"))
            changed = row is None or row[0] != code["body"]
            status = "current" if matches else ("in_progress" if row and row[1] == "in_progress" else "pending_code")
            attempts = 0 if matches or changed else (row[2] if row else 0)
            conn.execute(f"""INSERT INTO {functions} (fn_id, path, public, has_docstring, body_hash, docstring_hash,
                    signature_hash, doc_body_hash, doc_docstring_hash, doc_signature_hash, method_updated_at, status,
                    attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fn_id) DO UPDATE SET path=excluded.path, public=excluded.public,
                    has_docstring=excluded.has_docstring, body_hash=excluded.body_hash,
                    docstring_hash=excluded.docstring_hash, signature_hash=excluded.signature_hash,
                    doc_body_hash=excluded.doc_body_hash, doc_docstring_hash=excluded.doc_docstring_hash,
                    doc_signature_hash=excluded.doc_signature_hash, status=excluded.status, attempts=excluded.attempts,
                    method_updated_at=CASE WHEN {functions}.body_hash IS excluded.body_hash
                        THEN {functions}.method_updated_at ELSE excluded.method_updated_at END""",
                         (fn_id, code["path"], int(code["public"]), int(code["has_docstring"]), code["body"],
                          code["docstring"], code["signature"], page.get("body"), page.get("docstring"),
                          page.get("signature"), now if changed else None, status, attempts))
            if status != "current":
                pending.append(fn_id)
        for fn_id in gone:
            conn.execute(f"UPDATE {functions} SET status='orphan' WHERE fn_id=?", (fn_id,))
        for path, state in file_states.items():
            if state is None:
                conn.execute(f"DELETE FROM {files} WHERE path=?", (path,))
            else:
                conn.execute(f"INSERT OR REPLACE INTO {files} (path, mtime_ns, size) VALUES (?,?,?)", (path, *state))
    return pending


def claim(conn, root, limit, cost=lambda fn_id: 0, budget=None, now=None):
    """Take the oldest pending functions (attempts below MAX_ATTEMPTS) for one run: in_progress with a lease and one more
    attempt, at most `limit` of them and, when `budget` is given, while the summed `cost` (chain nodes) fits.

    Returns: list of fn_id claimed.
    """
    functions = tables(conn, root)[0]
    now = now or time.time()
    marks = ",".join("?" * len(PENDING))
    rows = conn.execute(f"SELECT fn_id FROM {functions} WHERE status IN ({marks}) AND attempts < ? "
                        "ORDER BY method_updated_at IS NULL, method_updated_at, fn_id", (*PENDING, MAX_ATTEMPTS)).fetchall()
    chosen, spent = [], 0
    for (fn_id,) in rows:
        if len(chosen) == limit:
            break
        weight = cost(fn_id)
        if budget is not None and chosen and spent + weight > budget:
            break
        chosen.append(fn_id)
        spent += weight
    with conn:
        conn.executemany(f"UPDATE {functions} SET status='in_progress', attempts=attempts+1, lease_until=? WHERE fn_id=?",
                         [(now + LEASE_S, fn_id) for fn_id in chosen])
    return chosen


def settle(conn, root, results, now=None):
    """Close a run: each claimed function whose page now carries its code stamps becomes current with the stamps and the
    date of the rewrite; the others go back to pending_code with the reason.

    Parameters: results (dict fn_id -> (stamps dict or None, error str or None)).
    """
    functions = tables(conn, root)[0]
    now = now or time.time()
    with conn:
        for fn_id, (stamps, error) in results.items():
            if stamps:
                conn.execute(f"UPDATE {functions} SET status='current', doc_signature_hash=?, doc_body_hash=?, "
                             "doc_docstring_hash=?, doc_updated_at=?, lease_until=NULL, last_error=NULL WHERE fn_id=?",
                             (stamps["signature"], stamps["body"], stamps["docstring"], now, fn_id))
            else:
                conn.execute(f"UPDATE {functions} SET status='pending_code', lease_until=NULL, last_error=? WHERE fn_id=?",
                             (error, fn_id))


def record_links(conn, root, links, now=None):
    """Replace the project's feature links with `links` (dicts feature_id, fn_id, layer, hops, origin, score, status)."""
    table = tables(conn, root)[2]
    now = now or time.time()
    with conn:
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(f"INSERT INTO {table} (feature_id, fn_id, layer, hops, origin, score, status, computed_at) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         [(l["feature_id"], l["fn_id"], l["layer"], l["hops"], l["origin"], l["score"], l["status"], now)
                          for l in links])


def rows(conn, root, status=None):
    """Registry rows of the project as dicts, optionally of one status."""
    functions = tables(conn, root)[0]
    cursor = conn.execute(f"SELECT * FROM {functions}" + (" WHERE status=?" if status else "") + " ORDER BY fn_id",
                          (status,) if status else ())
    return _as_dicts(cursor)


def _as_dicts(cursor):
    """The rows of an executed query as dicts keyed by column name."""
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def candidates(root):
    """Embedder candidate links (origin embedder, status candidate) of a project, read without creating anything: []
    when the registry file does not exist or does not track the project."""
    if not os.path.isfile(database_path()):
        return []
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE root=?", (root,)).fetchone():
            return []
        return [row for row in links(conn, root) if row["origin"] == "embedder" and row["status"] == "candidate"]
    finally:
        conn.close()


def links(conn, root):
    """Feature links of the project as dicts."""
    table = tables(conn, root)[2]
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY feature_id, fn_id")
    return _as_dicts(cursor)
