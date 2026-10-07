#!/usr/bin/env python3
"""Indexador hash-manifest do Smart Tool (stdlib only). Não faz nenhuma chamada de rede —
`reindex()` recebe `embed_batch` injetado pelo chamador (o daemon, que sabe qual modelo/
credencial usar). Vetores ficam como BLOB (`array` de float32), sem numpy; a busca faz
similaridade de cosseno em Python puro.
"""
import array
import collections
import hashlib
import json
import math
import operator
import os
import paths
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import atomic_io
import project_identity
import index_views
import embedding_cache
import document_text

INDEX_DIR = paths.DATA_DIR
CHUNK_LINES = 200
CHUNK_MAX_CHARS = 8000
CHUNK_OVERLAP_LINES = 12
CHUNKER_VERSION = 3
SCHEMA_VERSION = 2
REINDEX_BATCH_MAX_CHARS = 32_000
REINDEX_BATCH_MAX_ITEMS = 32
# Embedding requests in flight per flush. Measured 2026-10-03 (docs/spikes/embed_parallel_spike.py, 30 batches,
# 3 interleaved rounds): 1 worker 33-50 s, 2 workers 19-27 s, 4 workers 11-20 s (~2.8x), no 429 from the gateway.
EMBED_WORKERS = 4
# Weaviate's relativeScoreFusion (default since v1.24, DefaultAlpha 0.75). Measured 2026-10-05 on the rerank gold set
# (42 code targets, docs/spikes/fusion_spike.py) against the previous RRF k=60: target in the 20 candidates 41 -> 42,
# first place without rerank 31 -> 35, final MRR after rerank 0.946 -> 0.969.
FUSION_ALPHA = 0.75

# `read_indexable` centraliza os filtros entre o escopo e hash → chunk → embed →
# sqlite. `compute_manifest`, prévia e reindexação usam a mesma leitura; arquivos
# inalterados reaproveitam a decisão por stat. As regras são mecânicas de
# propósito — o escopo por LLM decide *diretórios* (convenção varia por repo), mas
# "isto é texto que vale embeddar" e "isto é material de credencial" não variam.
MAX_INDEXABLE_BYTES = 1024 * 1024
MAX_SVG_INDEXABLE_BYTES = 64 * 1024
_SNIFF_BYTES = 8192
# Linha média muito longa é bundle/JSON minificado: passa o sniff de binário (é ASCII),
# mas viraria dezenas de chunks todos com start_line=1, inúteis como citação.
MAX_AVG_LINE_CHARS = 2000

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF8_BOM = b"\xef\xbb\xbf"

# Nunca indexado: o conteúdo vai para um endpoint externo de embeddings e fica em texto
# puro no sqlite3, e nenhuma das duas coisas se desfaz. Comparação sempre em minúsculas
# — o sistema de arquivos do Windows não distingue caixa, e um `set` distinguiria.
_SECRET_BASENAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity", "credentials",
    "credentials.json", "service-account.json", "kubeconfig", "netrc", "_netrc",
    "npmrc", "pgpass", "htpasswd", "master.key", "secrets.json", "secrets.yaml",
    "secrets.yml", "local.settings.json",
})
_SECRET_EXTS = frozenset({
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk", ".kdbx", ".asc",
    ".gpg", ".env", ".tfvars", ".har",
})
# Sobrevive a renome — é a única regra que não depende de adivinhar convenção alheia.
# Só vale no cabeçalho: arquivo de credencial ABRE com o marcador, enquanto código que
# trata desses formatos (este módulo, um scanner de segredo, um teste) menciona o mesmo
# literal no meio — e recusar esse código seria tirar fonte legítima do índice.
_SECRET_CONTENT_MARKERS = (
    b"PRIVATE KEY-----", b"PuTTY-User-Key-File", b"aws_secret_access_key",
)
_SECRET_MARKER_WINDOW = 512

SKIP_DOTFILE = "dotfile"
SKIP_SECRET = "credential"
SKIP_TOO_LARGE = "over size limit"
SKIP_GRAPHIC = "large svg graphic"
SKIP_BINARY = "binary"
SKIP_MINIFIED = "minified"
SKIP_UNREADABLE = "unreadable"


LEXICAL_MODEL = "lexical"

def _secret_name(name):
    if name in _SECRET_BASENAMES:
        return True
    if os.path.splitext(name)[1] in _SECRET_EXTS:
        return True
    # `local.env`/`prod.env` caem na extensão; `env`/`env.local` não têm extensão nenhuma.
    return name == "env" or name.startswith("env.")


def _text_encoding(prefix):
    """Encoding de leitura deduzido do BOM. UTF-16 é o default de `Out-File` no
    PowerShell 5.1: decodificar como UTF-8 entregaria texto intercalado de `\\x00`."""
    if prefix.startswith(_UTF16_BOMS):
        return "utf-16"
    if prefix.startswith(_UTF8_BOM):
        return "utf-8-sig"
    return "utf-8"


def read_indexable(root, rel_path):
    """`(bytes, encoding)` do arquivo, ou `(None, motivo)` quando ele não pode ser
    indexado. Uma única leitura serve o hash do manifest e o chunking; arquivo grande e
    binário são recusados antes de qualquer `read` integral."""
    full = os.path.join(root, rel_path)
    if not project_identity.within_root(root, full):
        return None, SKIP_SECRET
    parts = str(rel_path).replace("\\", "/").split("/")
    if any(part.startswith(".") for part in parts if part not in (".", "..")):
        return None, SKIP_DOTFILE
    name = os.path.basename(rel_path).lower()
    # Nome iniciado por ponto: mesma regra que `index_scope` aplica a diretório, e é
    # onde vive metade dos arquivos de credencial (.env, .npmrc, .netrc).
    if name.startswith("."):
        return None, SKIP_DOTFILE
    if _secret_name(name):
        return None, SKIP_SECRET
    try:
        size = os.path.getsize(full)
        if name.endswith(".svg") and size > MAX_SVG_INDEXABLE_BYTES:
            return None, SKIP_GRAPHIC
        limit = document_text.MAX_ARCHIVE_BYTES if name.endswith('.docx') else MAX_INDEXABLE_BYTES
        if size > limit:
            return None, SKIP_TOO_LARGE
        with open(full, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None, SKIP_UNREADABLE
    if len(data) > limit:
        return None, SKIP_TOO_LARGE
    if name.endswith('.docx'):
        try:
            data = document_text.extract_docx(data)
        except document_text.DocumentError as exc:
            return None, str(exc)
    prefix = data[:_SNIFF_BYTES]
    encoding = _text_encoding(prefix)
    if encoding == "utf-16":
        try:
            prefix = data.decode("utf-16", "ignore")[:_SNIFF_BYTES].encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None, SKIP_BINARY
    elif b"\0" in prefix:
        return None, SKIP_BINARY
    header = prefix[:_SECRET_MARKER_WINDOW]
    if any(marker in header for marker in _SECRET_CONTENT_MARKERS):
        return None, SKIP_SECRET
    newlines = prefix.count(b"\n")
    if newlines == 0 and len(prefix) >= _SNIFF_BYTES:
        return None, SKIP_MINIFIED
    if newlines and len(prefix) / (newlines + 1) > MAX_AVG_LINE_CHARS:
        return None, SKIP_MINIFIED
    return data, encoding


def is_indexable(root, rel_path):
    return read_indexable(root, rel_path)[0] is not None


def _project_hash(root):
    return index_views.storage_key(root)


def db_path(root):
    os.makedirs(INDEX_DIR, exist_ok=True)
    return os.path.join(INDEX_DIR, _project_hash(root) + ".sqlite3")


_MIGRATION_LOCK = threading.RLock()
_VECTOR_CACHE_LOCK = threading.RLock()
_VECTOR_CACHE = collections.OrderedDict()
VECTOR_CACHE_MAX_BYTES = 64 * 1024 * 1024
VECTOR_CACHE_MAX_PROJECTS = 3


class IndexCompatibilityError(RuntimeError):
    """Modelo/dimensão do índice não correspondem à consulta."""


class IndexVectorError(RuntimeError):
    """Resposta de embeddings fora do contrato; lote não pode ser confirmado."""


def existing_db_path(root):
    canonical = os.path.join(INDEX_DIR, _project_hash(root) + ".sqlite3")
    if os.path.isfile(canonical):
        return canonical
    if index_views.describe(root).get('git'):
        return None  # Um banco sem visão conhecida não pode responder por uma branch.
    for key in project_identity.legacy_ids(root):
        old = os.path.join(INDEX_DIR, key + ".sqlite3")
        if os.path.isfile(old):
            return old
    return None


def _readonly(path):
    return sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10)


def _backup(source, target):
    target = project_identity.state_path(target, INDEX_DIR)
    temp = atomic_io.tmp_path_for(target)
    src, dst = _readonly(source), sqlite3.connect(temp)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        src.close()
        dst.close()
    atomic_io.atomic_replace(temp, target)


def migrate_legacy(root):
    """Cópia consistente para a chave canônica; o banco legado permanece intacto."""
    target = db_path(root)
    with _MIGRATION_LOCK:
        if not os.path.isfile(target):
            old = existing_db_path(root)
            if not old and index_views.describe(root).get('git'):
                old = next((os.path.join(INDEX_DIR, key + '.sqlite3') for key in
                            [project_identity.project_id(root), *project_identity.legacy_ids(root)]
                            if os.path.isfile(os.path.join(INDEX_DIR, key + '.sqlite3'))), None)
            if old:
                _backup(old, target)
    return target


def _open_path(path):
    conn = sqlite3.connect(path, timeout=10)
    # `chunks.text` é conteúdo de arquivo em texto puro: sem isto, um DELETE só desliga a
    # página e o texto continua legível nas free pages do arquivo até um VACUUM.
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS manifest (path TEXT PRIMARY KEY, hash TEXT NOT NULL, "
        "mtime_ns INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT -1)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, "
        "text TEXT NOT NULL, embedding BLOB)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
    conn.execute("CREATE TABLE IF NOT EXISTS file_state(path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, reason TEXT NOT NULL)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(manifest)")}
    for column, sql_type, default in (("mtime_ns", "INTEGER", "0"), ("size", "INTEGER", "-1")):
        if column not in columns:
            conn.execute(f"ALTER TABLE manifest ADD COLUMN {column} {sql_type} NOT NULL DEFAULT {default}")
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    try:
        existed = conn.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_fts'").fetchone()
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(path, text, content='chunks', content_rowid='id')")
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid,path,text) VALUES(new.id,new.path,new.text); END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts,rowid,path,text) VALUES('delete',old.id,old.path,old.text); END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts,rowid,path,text) VALUES('delete',old.id,old.path,old.text);
                INSERT INTO chunks_fts(rowid,path,text) VALUES(new.id,new.path,new.text); END;
        """)
        if not existed:
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        _set_metadata(conn, {"fts": True})
    except sqlite3.OperationalError:
        _set_metadata(conn, {"fts": False})
    conn.commit()
    return conn


def open_db(root):
    return _open_path(migrate_legacy(root))


def _metadata(conn):
    try:
        return {key: json.loads(value) for key, value in conn.execute("SELECT key,value FROM metadata")}
    except (sqlite3.Error, ValueError):
        return {}


def _set_metadata(conn, values):
    conn.executemany("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE metadata.value != excluded.value",
                     [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()])


def info(root):
    path = existing_db_path(root)
    if not path:
        return {"exists": False, "path": os.path.join(INDEX_DIR, _project_hash(root) + ".sqlite3")}
    try:
        conn = _readonly(path)
        try:
            meta = _metadata(conn)
            return {**meta, "exists": True, "path": path, "bytes": os.path.getsize(path),
                    "files": conn.execute("SELECT count(*) FROM manifest").fetchone()[0],
                    "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
                    "legacy": not bool(meta.get("model_id"))}
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return {"exists": True, "path": path, "error": type(exc).__name__, "legacy": True}


def manifest_snapshot(root):
    path = existing_db_path(root)
    if not path:
        return {}
    conn = _readonly(path)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_state'").fetchone():
            return {rel: (mtime, size) for rel, mtime, size in conn.execute("SELECT path,mtime_ns,size FROM file_state")}
        return {rel: (mtime, size) for rel, mtime, size in conn.execute("SELECT path,mtime_ns,size FROM manifest")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def compute_manifest(root, included_paths):
    """`(manifest, contagem de descartes por motivo)`. O manifest é a fonte de verdade do
    cálculo completo de hashes. A reindexação usa `read_indexable` com os mesmos gates
    ao detectar alteração por stat/evento; arquivos que deixam de se qualificar saem
    do manifest e têm os chunks removidos."""
    manifest = {}
    skipped = {}
    for rel in included_paths:
        data, reason = read_indexable(root, rel)
        if data is None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        manifest[rel] = hashlib.sha256(data).hexdigest()
    return manifest, skipped


def load_manifest(conn):
    return dict(conn.execute("SELECT path, hash FROM manifest").fetchall())


def diff_manifest(old, new):
    added = [p for p in new if p not in old]
    changed = [p for p in new if p in old and old[p] != new[p]]
    removed = [p for p in old if p not in new]
    return added, changed, removed


def _split_oversized(start_line, end_line, text, max_chars):
    return [
        (start_line, end_line, text[i:i + max_chars])
        for i in range(0, len(text), max_chars)
    ]


def chunk_file(root, rel_path, max_lines=CHUNK_LINES, max_chars=CHUNK_MAX_CHARS, data=None, encoding=None):
    """`data`/`encoding` já resolvidos por `read_indexable` evitam reler o arquivo. Sem
    eles, relê e redecide o encoding — e uma leitura que falhe devolve `None`, distinto
    de `[]` (arquivo legítimo sem conteúdo), porque quem chama não pode gravar hash novo
    de um conteúdo que não conseguiu ler."""
    if data is None:
        data, encoding = read_indexable(root, rel_path)
        if data is None:
            return None
    try:
        text = data.decode(encoding or "utf-8", "ignore")
    except (LookupError, UnicodeDecodeError):
        return None
    lines = text.splitlines(keepends=True)
    if max_lines < 1 or max_chars < 1:
        raise ValueError("Chunk limits must be positive.")
    boundary = re.compile(r"^(?:\s*(?:async\s+)?(?:def|class|function)\s|export\s|#{1,6}\s)")
    chunks, start = [], 0
    while start < len(lines):
        if len(lines[start]) > max_chars:
            chunks.extend(_split_oversized(start + 1, start + 1, lines[start], max_chars))
            start += 1
            continue
        end, chars = start, 0
        while end < len(lines) and end - start < max_lines and chars + len(lines[end]) <= max_chars:
            chars += len(lines[end])
            end += 1
        # Prefere terminar antes de uma nova função/seção, mantendo os limites reais
        # de linha em cada fragmento. Uma pequena sobreposição preserva contexto.
        if end < len(lines) and end - start > 40:
            breaks = [i for i in range(start + 30, end) if boundary.match(lines[i])]
            if breaks:
                end = breaks[-1]
        chunks.append((start + 1, end, "".join(lines[start:end])))
        if end == len(lines):
            break
        overlap = min(CHUNK_OVERLAP_LINES, (end - start) // 5)
        start = max(start + 1, end - overlap)
    return chunks


def _pack_vector(vector):
    validate_vector(vector)
    try:
        packed = array.array("f", vector)
    except (OverflowError, TypeError, ValueError) as exc:
        raise IndexVectorError("Embedding does not fit in float32.") from exc
    if not all(math.isfinite(value) for value in packed):
        raise IndexVectorError("Embedding contains a non-finite float32 value.")
    return packed.tobytes()


def _unpack_vector(blob):
    arr = array.array("f")
    arr.frombytes(blob)
    return arr


_sumprod = getattr(math, "sumprod", None) or (lambda a, b: sum(map(operator.mul, a, b)))


def cosine_similarity(a, b):
    if len(a) != len(b):
        raise IndexCompatibilityError(f"Incompatible dimensions: query={len(a)}, index={len(b)}.")
    norm_a = math.sqrt(_sumprod(a, a))
    norm_b = math.sqrt(_sumprod(b, b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return _sumprod(a, b) / (norm_a * norm_b)


def validate_vector(vector, dimensions=None):
    if not isinstance(vector, (list, tuple, array.array)) or not vector:
        raise IndexVectorError("Embedding must be a non-empty numeric vector.")
    if dimensions not in (None, 0) and len(vector) != dimensions:
        raise IndexCompatibilityError(f"Embedding has {len(vector)} dimensions; expected {dimensions}.")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in vector):
        raise IndexVectorError("Embedding contains an invalid or non-finite value.")
    return len(vector)


def replace_file_chunks(conn, rel_path, chunks_with_vectors):
    conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
    conn.executemany(
        "INSERT INTO chunks (path, start_line, end_line, text, embedding) VALUES (?, ?, ?, ?, ?)",
        [
            (rel_path, start, end, text, None if vector is None else _pack_vector(vector))
            for (start, end, text), vector in chunks_with_vectors
        ],
    )


def remove_file(conn, rel_path):
    conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM manifest WHERE path = ?", (rel_path,))


def _file_state(conn, rel_path, stat, reason="indexed"):
    conn.execute("INSERT INTO file_state VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                 "mtime_ns=excluded.mtime_ns,size=excluded.size,reason=excluded.reason",
                 (rel_path, *stat, reason))


def upsert_manifest_entry(conn, rel_path, file_hash, mtime_ns=0, size=-1):
    conn.execute(
        "INSERT INTO manifest (path, hash, mtime_ns, size) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, mtime_ns=excluded.mtime_ns, size=excluded.size",
        (rel_path, file_hash, mtime_ns, size),
    )
    _file_state(conn, rel_path, (mtime_ns, size))


def _stat_candidate(root, rel):
    full = os.path.join(root, rel)
    name = os.path.basename(rel).lower()
    if not project_identity.within_root(root, full):
        return None, SKIP_SECRET
    if any(part.startswith(".") for part in str(rel).replace("\\", "/").split("/")
           if part not in (".", "..")):
        return None, SKIP_DOTFILE
    if _secret_name(name):
        return None, SKIP_SECRET
    try:
        stat = os.stat(full)
        if not os.path.isfile(full):
            return None, SKIP_UNREADABLE
    except OSError:
        return None, SKIP_UNREADABLE
    if stat.st_size > (document_text.MAX_ARCHIVE_BYTES if name.endswith('.docx') else MAX_INDEXABLE_BYTES):
        return None, SKIP_TOO_LARGE
    if name.endswith(".svg") and stat.st_size > MAX_SVG_INDEXABLE_BYTES:
        return None, SKIP_GRAPHIC
    return (stat.st_mtime_ns, stat.st_size), None


def _stable_read(root, rel):
    for _ in range(2):
        before, reason = _stat_candidate(root, rel)
        if before is None:
            return None, reason, None
        data, encoding = read_indexable(root, rel)
        if data is None:
            return None, encoding, before
        after, _ = _stat_candidate(root, rel)
        if after == before and (str(rel).lower().endswith('.docx') or len(data) == before[1]):
            return data, encoding, before
    return None, "file changed while reading", None


def invalidate_cache(root):
    with _VECTOR_CACHE_LOCK:
        for key in list(_VECTOR_CACHE):
            if key[0] == project_identity.canonical_root(root):
                _VECTOR_CACHE.pop(key, None)


def reindex(root, included_paths, embed_batch, *args, **kwargs):
    with index_views.pin(root) as view:
        return _reindex(root, included_paths, embed_batch, *args, **kwargs)


def _split_batches(texts, max_items, max_chars):
    batches, batch, size = [], [], 0
    for text in texts:
        if batch and (len(batch) >= max_items or size + len(text) > max_chars):
            batches.append(batch)
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        batches.append(batch)
    return batches


def _reindex(root, included_paths, embed_batch, chunk_lines=CHUNK_LINES, on_progress=None,
            batch_max_chars=REINDEX_BATCH_MAX_CHARS, batch_max_items=REINDEX_BATCH_MAX_ITEMS,
            model_id=None, expected_dimensions=None, force_rebuild=False, full_check=False,
            changed_paths=None, cancel_check=None):
    """Atualiza snapshots por arquivo; um modelo novo só substitui a geração após sucesso."""
    if batch_max_chars < 1 or batch_max_items < 1:
        raise ValueError("Indexing batch limits must be positive.")
    view = index_views.describe(root)
    def check():
        if cancel_check:
            cancel_check()
        index_views.assert_current(view)
    root = project_identity.display_root(root)
    model_id = model_id or "unspecified"
    target = migrate_legacy(root)
    current = info(root)
    # Checkout e commit exigem confronto de conteúdo, mesmo com timestamps iguais.
    if view.get('git') and (current.get('git_commit') != view.get('commit') or
                            current.get('view_id') != view.get('view_id')):
        full_check = True
    compatible = (current.get("model_id") == model_id and
                  current.get("chunker_version") == CHUNKER_VERSION and
                  current.get("schema_version") == SCHEMA_VERSION and
                  current.get("chunk_lines") == chunk_lines and
                  (expected_dimensions is None or current.get("dimensions") in (0, expected_dimensions)))
    building = force_rebuild or not compatible
    signature = hashlib.sha256(json.dumps([model_id, expected_dimensions, CHUNKER_VERSION,
                                           SCHEMA_VERSION, chunk_lines]).encode()).hexdigest()[:12]
    work_path = target[:-8] + f".building-{signature}.sqlite3" if building else target
    project_identity.state_path(work_path, INDEX_DIR)
    conn = _open_path(work_path)
    stats = {"added": 0, "changed": 0, "removed": 0, "failed": 0,
             "unchanged": 0, "skipped": {}, "rebuilt": building, "ready": False}
    cache = None
    try:
        meta = _metadata(conn)
        if meta.get("model_id") not in (None, model_id):
            raise IndexCompatibilityError("Temporary generation belongs to another model.")
        dimensions = meta.get("dimensions") or expected_dimensions or 0
        contract = {'model': model_id, 'dimensions': dimensions, 'chunker': CHUNKER_VERSION,
                    'chunk_lines': chunk_lines, 'preprocessing': 1}
        cache = embedding_cache.VectorCache(root, INDEX_DIR, contract)
        # Alimenta o cache com o banco existente somente quando o contrato é conhecido.
        if compatible and dimensions and not meta.get('vector_cache_seeded'):
            for text, blob in conn.execute('SELECT text,embedding FROM chunks WHERE embedding IS NOT NULL'):
                vector = _unpack_vector(blob)
                validate_vector(vector, dimensions)
                cache.put(text, vector)
            cache.db.commit()
        _set_metadata(conn, {'vector_cache_seeded': True})
        generation = meta.get("generation") or uuid.uuid4().hex
        _set_metadata(conn, {"root": project_identity.canonical_root(root), "model_id": model_id,
                            "dimensions": dimensions, "schema_version": SCHEMA_VERSION,
                            "chunker_version": CHUNKER_VERSION, "chunk_lines": chunk_lines,
                            "generation": generation})
        conn.commit()
        old = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT path,hash,mtime_ns,size FROM manifest")}
        old_state = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path,mtime_ns,size FROM file_state")}
        observed, candidates = {}, []
        forced = {str(p).replace("\\", "/").casefold().strip("/") for p in (changed_paths or [])}
        for rel in sorted(set(included_paths)):
            check()
            stat, reason = _stat_candidate(root, rel)
            if stat is None:
                stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
                continue
            observed[rel] = stat
            force_file = any(rel.casefold() == p or rel.casefold().startswith(p + "/") for p in forced)
            extractor_changed = rel.lower().endswith('.docx') and meta.get('document_extractor_version') != document_text.VERSION
            if (full_check or building or force_file or extractor_changed or old_state.get(rel) != stat):
                candidates.append(rel)
            else:
                stats["unchanged"] += 1
        for rel in set(old) - set(observed):
            remove_file(conn, rel)
            stats["removed"] += 1
        for rel in set(old_state) - set(observed):
            conn.execute("DELETE FROM file_state WHERE path=?", (rel,))
        conn.commit()
        processed, total = 0, len(candidates)
        if on_progress:
            on_progress(0, total)
        pending, texts, chars = [], [], 0

        def progress():
            if on_progress:
                on_progress(processed, total)

        def flush():
            nonlocal pending, texts, chars, processed, dimensions
            if not pending:
                return
            if embed_batch is None:
                vectors = [None] * len(texts)
            else:
                def guarded(part):
                    check()
                    return embed_batch(part)

                check()
                vectors = cache.embed(texts, guarded, validate_vector, use_cache=not force_rebuild,
                                      split=lambda items: _split_batches(items, batch_max_items, batch_max_chars),
                                      workers=EMBED_WORKERS)
                check()
                if len(vectors) != len(texts):
                    raise IndexVectorError("Embedding count differs from the batch sent.")
                for vector in vectors:
                    dimension = validate_vector(vector, dimensions)
                    if not dimensions:
                        dimensions = dimension
            offset = 0
            for rel, chunks, digest, stat in pending:
                check()
                count = len(chunks)
                replace_file_chunks(conn, rel, zip(chunks, vectors[offset:offset + count]))
                upsert_manifest_entry(conn, rel, digest, *stat)
                _set_metadata(conn, {"dimensions": dimensions, "updated_at": time.time()})
                conn.commit()
                offset += count
                processed += 1
                progress()
            pending, texts, chars = [], [], 0

        for rel in candidates:
            check()
            data, encoding, stat = _stable_read(root, rel)
            if data is None:
                flush()
                stats["skipped"][encoding] = stats["skipped"].get(encoding, 0) + 1
                if encoding in (SKIP_UNREADABLE, "file changed while reading"):
                    stats["failed"] += 1
                elif stat is not None:
                    _file_state(conn, rel, stat, encoding)
                if rel in old:
                    remove_file(conn, rel)
                conn.commit()
                processed += 1
                progress()
                continue
            digest = hashlib.sha256(data).hexdigest()
            if rel in old and digest == old[rel][0]:
                upsert_manifest_entry(conn, rel, digest, *stat)
                conn.commit()
                stats["unchanged"] += 1
                processed += 1
                progress()
                continue
            chunks = chunk_file(root, rel, chunk_lines, data=data, encoding=encoding)
            stats["changed" if rel in old else "added"] += 1
            count_chars = sum(len(chunk[2]) for chunk in chunks)
            if pending and (len(texts) + len(chunks) > batch_max_items * EMBED_WORKERS
                            or chars + count_chars > batch_max_chars * EMBED_WORKERS):
                flush()
            pending.append((rel, chunks, digest, stat))
            texts.extend(chunk[2] for chunk in chunks)
            chars += count_chars
            if len(texts) >= batch_max_items * EMBED_WORKERS or chars >= batch_max_chars * EMBED_WORKERS:
                flush()
        flush()
        check()
        if full_check or building:
            _set_metadata(conn, {"full_scan_at": time.time()})
        _set_metadata(conn, {"dimensions": dimensions})
        _set_metadata(conn, {'document_extractor_version':document_text.VERSION})
        _set_metadata(conn, {'view_id': view['view_id'], 'git_branch': view.get('branch'),
                             'git_commit': view.get('commit'), 'view_label': view['label']})
        if building or stats["added"] or stats["changed"] or stats["removed"]:
            _set_metadata(conn, {"updated_at": time.time()})
        conn.commit()
        stats.update(generation=generation, dimensions=dimensions,
                     embedded_chunks=cache.misses, reused_chunks=cache.hits,
                     embedded_characters=cache.characters, view=index_views.public(view),
                     files=conn.execute("SELECT count(*) FROM manifest").fetchone()[0],
                     chunks=conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
        if cache:
            cache.close()
    check()
    if building and not stats["failed"]:
        if os.path.isfile(target):
            _backup(target, target[:-8] + ".previous.sqlite3")
        atomic_io.atomic_replace(work_path, project_identity.state_path(target, INDEX_DIR))
    stats["ready"] = not bool(stats["failed"])
    stats["path"] = target if stats["ready"] else work_path
    if building or total or stats["removed"] or full_check:
        invalidate_cache(root)
    return stats


def _vector_rows(root, path):
    stat = os.stat(path)
    key = (project_identity.canonical_root(root), path, stat.st_mtime_ns, stat.st_size)
    with _VECTOR_CACHE_LOCK:
        cached = _VECTOR_CACHE.get(key)
        if cached is not None:
            _VECTOR_CACHE.move_to_end(key)
            return cached[0], cached[1], cached[3]
    conn = _readonly(path)
    try:
        meta = _metadata(conn)
        raw = conn.execute("SELECT id,path,start_line,end_line,text,embedding FROM chunks").fetchall()
    finally:
        conn.close()
    rows, norms, byte_count = [], {}, 0
    for chunk_id, file_path, start, end, text, blob in raw:
        vector = _unpack_vector(blob) if blob else array.array("f")
        if vector:
            validate_vector(vector, meta.get("dimensions"))
            norms[chunk_id] = math.sqrt(_sumprod(vector, vector))
        rows.append((chunk_id, file_path, start, end, text, vector))
        byte_count += len(text.encode("utf-8")) + len(blob or b"") + 256
    after = os.stat(path)
    if byte_count <= VECTOR_CACHE_MAX_BYTES and (stat.st_mtime_ns, stat.st_size) == (after.st_mtime_ns, after.st_size):
        with _VECTOR_CACHE_LOCK:
            invalidate_cache(root)
            _VECTOR_CACHE[key] = (rows, meta, byte_count, norms)
            while (len(_VECTOR_CACHE) > VECTOR_CACHE_MAX_PROJECTS or
                   sum(item[2] for item in _VECTOR_CACHE.values()) > VECTOR_CACHE_MAX_BYTES):
                _VECTOR_CACHE.popitem(last=False)
    return rows, meta, norms


def _lexical_scored(path, query, rows, limit, offset=0):
    """(chunk_id, score) best first; higher score is a better lexical match."""
    terms = list(dict.fromkeys(re.findall(r"[\w]{2,}", query or "", re.UNICODE)))[:24]
    if not terms:
        return []
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    conn = _readonly(path)
    try:
        return [(row[0], -row[1]) for row in conn.execute(
            "SELECT rowid, bm25(chunks_fts,3.0,1.0) FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts,3.0,1.0) LIMIT ? OFFSET ?",
            (expression, limit, offset))]
    except sqlite3.OperationalError:
        # FTS5 indisponível em algum Python: mantém recuperação lexical em stdlib.
        ranked = [(sum(term.casefold() in (file_path + " " + text).casefold() for term in terms), chunk_id)
                  for chunk_id, file_path, _start, _end, text, _vector in rows]
        return [(chunk_id, score) for score, chunk_id in sorted(ranked, reverse=True)[offset:offset + limit] if score]
    finally:
        conn.close()


def _min_max(pairs):
    if not pairs:
        return {}
    values = [value for _, value in pairs]
    low, high = min(values), max(values)
    return {key: (value - low) / (high - low) if high > low else 1.0 for key, value in pairs}


def search(root, query_vector=None, top_k=20, query=None, model_id=None, include=None):
    """Recuperação vetorial + lexical por fusão de notas relativas, sem rede."""
    path = existing_db_path(root)
    if not path:
        return []
    rows, meta, norms = _vector_rows(root, path)
    if include is not None:
        rows = [row for row in rows if include(row[1])]
    if model_id and meta.get("model_id") != model_id:
        raise IndexCompatibilityError("The index must be rebuilt for the selected model.")
    dimensions = meta.get("dimensions")
    vector_scores = {}
    if query_vector is not None:
        validate_vector(query_vector, dimensions)
        query_norm = math.sqrt(_sumprod(query_vector, query_vector))
        for chunk_id, _path, _start, _end, _text, vector in rows:
            if vector:
                if len(vector) != len(query_vector):
                    raise IndexCompatibilityError(f"Incompatible dimensions: query={len(query_vector)}, index={len(vector)}.")
                norm = norms[chunk_id] * query_norm
                vector_scores[chunk_id] = _sumprod(query_vector, vector) / norm if norm else 0.0
    vector_ids = sorted(vector_scores, key=vector_scores.get, reverse=True)
    limit = max(top_k * 4, 40)
    if not query:
        lexical = []
    elif include is None:
        lexical = _lexical_scored(path, query, rows, limit)
    else:
        allowed, lexical, offset, page = {row[0] for row in rows}, [], 0, limit * 10
        while len(lexical) < limit:
            batch = _lexical_scored(path, query, rows, page, offset)
            lexical += [pair for pair in batch if pair[0] in allowed]
            if len(batch) < page:
                break
            offset += page
        lexical = lexical[:limit]
    if lexical:
        vector_part = _min_max([(chunk_id, vector_scores[chunk_id]) for chunk_id in vector_ids[:limit]])
        lexical_part = _min_max(lexical)
        alpha = FUSION_ALPHA if vector_part else 0.0
        scores = {chunk_id: alpha * vector_part.get(chunk_id, 0.0) + (1 - alpha) * lexical_part.get(chunk_id, 0.0)
                  for chunk_id in set(vector_part) | set(lexical_part)}
        ids = sorted(scores, key=lambda item: (scores[item], item in lexical_part), reverse=True)
    else:
        scores, ids = vector_scores, vector_ids
    by_id = {row[0]: row for row in rows}
    return [(scores[chunk_id], *by_id[chunk_id][1:5]) for chunk_id in ids[:top_k]]


def remove_index(root):
    """Remove somente arquivos de índice da raiz conhecida, nunca arquivos do projeto."""
    import glob
    removed = []
    for key in set([project_identity.project_id(root), *project_identity.legacy_ids(root)]):
        pattern = re.compile(re.escape(key) + r"(?:\.v-[a-f0-9]{16})?(?:\.previous|\.building-[a-f0-9]+|\.vectors)?\.sqlite3$")
        for path in glob.glob(os.path.join(INDEX_DIR, key + "*.sqlite3")):
            if pattern.fullmatch(os.path.basename(path)):
                safe = project_identity.state_path(path, INDEX_DIR)
                try:
                    os.remove(safe)
                except PermissionError as exc:
                    raise RuntimeError("The index is open in another process. Close the external reader and try removing it again.") from exc
                removed.append(safe)
    invalidate_cache(root)
    return removed


def copy_project_indexes(root, destination):
    """Copia todas as visões e cache ao reassociar uma raiz, preservando a origem."""
    import glob
    target_key = project_identity.project_id(destination)
    copied = set()
    for key in dict.fromkeys([project_identity.project_id(root), *project_identity.legacy_ids(root)]):
        pattern = re.compile(re.escape(key) + r'((?:\.v-[a-f0-9]{16})?(?:\.previous|\.building-[a-f0-9]+|\.vectors)?\.sqlite3)$')
        for path in glob.glob(os.path.join(INDEX_DIR, key + '*.sqlite3')):
            match = pattern.fullmatch(os.path.basename(path))
            if match:
                target = os.path.join(INDEX_DIR, target_key + match[1])
                if target not in copied:
                    if os.path.exists(target):
                        raise ValueError('The reassociation target already has data.')
                    _backup(path, target)
                    copied.add(target)
    return sorted(copied)
