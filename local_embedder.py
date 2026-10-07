"""Embedder local do mcp-memory (sidecar granite em ~/.mcp-memory/embedding.properties) como reserva sem modelo no gateway.

Medido em 2026-10-05 no gabarito do rerank (42 alvos de código, sem rerank, docs/spikes/local_embedder_spike.py):
granite-embedding-107m-multilingual local 42/35/41 (no pool / 1º / top 5), MRR@5 0,892; gateway
text-embedding-3-small do gateway 42/35/40, 0,885; só lexical 38/29/36, 0,767.
"""
import http.client
import json
import os
import threading
import time
import urllib.parse

import config
import indexer

PREFIX = "local:"
PROPERTIES = os.path.join(os.path.expanduser("~"), ".mcp-memory", "embedding.properties")
BATCH = 16
INFO_TTL_S = 60
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_S = 60
_lock = threading.Lock()
_info = {"at": 0.0, "value": None}


def url():
    """Sidecar base URL; None when absent or `tei.url=off`. localhost becomes 127.0.0.1: on Windows ::1 is tried
    first and a sidecar bound to IPv4 only costs ~2 s per connection (measured)."""
    try:
        with open(PROPERTIES, encoding="utf-8") as stream:
            for line in stream:
                key, _, value = line.strip().partition("=")
                value = value.strip()
                if key.strip() == "tei.url":
                    if not value or value.lower() == "off":
                        return None
                    parsed = urllib.parse.urlsplit(value.rstrip("/"))
                    if parsed.hostname == "localhost":
                        parsed = parsed._replace(netloc=parsed.netloc.replace("localhost", "127.0.0.1", 1))
                    return urllib.parse.urlunsplit(parsed)
    except OSError:
        return None
    return None


def configured():
    return url() is not None


def _connection(base, timeout):
    parsed = urllib.parse.urlsplit(base)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return cls(parsed.hostname, parsed.port, timeout=timeout)


def _request(conn, method, path, body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers)
    response = conn.getresponse()
    data = response.read()
    if response.status >= 400:
        raise ConnectionError(f"local embedder answered HTTP {response.status}")
    return json.loads(data)


def _probe(base, timeout):
    conn = _connection(base, timeout)
    try:
        data = _request(conn, "GET", "/info")
    finally:
        conn.close()
    if not data.get("model_id") or int(data.get("dimension") or 0) <= 0:
        raise ValueError("local embedder has no model_id/dimension in /info")
    return {"model": PREFIX + data["model_id"], "dimensions": int(data["dimension"]), "url": base,
            "sha256": data.get("model_sha256")}


def info(timeout=1.5):
    """Sidecar /info cached for INFO_TTL_S; None when not configured or not answering. The probe runs under the lock
    so a slow failing probe never overwrites a newer good answer."""
    with _lock:
        if time.monotonic() - _info["at"] < INFO_TTL_S:
            return _info["value"]
        base, value = url(), None
        if base:
            try:
                value = _probe(base, timeout)
            except (OSError, ValueError, http.client.HTTPException):
                value = None
        _info.update(at=time.monotonic(), value=value)
        return value


def is_local(model):
    return bool(model) and model.startswith(PREFIX)


def resolve(configured_model, current=None, mode=None):
    """Embedding model for the index, following config `local_models` (fallback | off | prefer).

    An index already built with the local model is kept while the sidecar is briefly down, so a restart of the
    sidecar never triggers a rebuild to lexical and back."""
    mode = mode or config.local_models_mode()
    if mode == "off":
        return configured_model or indexer.LEXICAL_MODEL
    if configured_model and mode == "fallback":
        return configured_model
    available = info()
    if available:
        return available["model"]
    if is_local(current):
        return current
    return configured_model or indexer.LEXICAL_MODEL


def _left(deadline):
    if deadline is None:
        return REQUEST_TIMEOUT_S
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Deadline exceeded before local embeddings finished.")
    return min(REQUEST_TIMEOUT_S, left)


def embed(model, texts, deadline=None, cancel_check=None, on_retry=None):
    base = url()
    if not base:
        raise RuntimeError(f"mcp-memory local embedder is not configured ({PROPERTIES}).")
    # Fresh /info before and after: /embed does not echo the model, and a cached answer could let vectors of a
    # different model reach the persistent vector cache under this model's contract.
    try:
        before = _probe(base, _left(deadline))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise RuntimeError(f"mcp-memory local embedder is not responding ({base}): {exc}") from None
    if before["model"] != model:
        raise indexer.IndexCompatibilityError(
            f"The local embedder is now {before['model']}; the index was built with {model} and must be rebuilt.")
    vectors = []
    conn = _connection(base, _left(deadline))
    try:
        for start in range(0, len(texts), BATCH):
            part = texts[start:start + BATCH]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if cancel_check:
                    cancel_check()
                conn.timeout = _left(deadline)
                try:
                    got = _request(conn, "POST", "/embed", {"inputs": part})
                    break
                except (OSError, http.client.HTTPException) as exc:
                    conn.close()
                    conn = _connection(base, _left(deadline))
                    if attempt == MAX_ATTEMPTS:
                        raise RuntimeError(f"Local embedder failed after {MAX_ATTEMPTS} attempts: {exc}") from None
                    if on_retry:
                        on_retry(attempt + 1, len(part))
                    delay = attempt
                    if deadline is not None and time.monotonic() + delay >= deadline:
                        raise TimeoutError("Not enough time left to retry the local embedder.") from None
                    time.sleep(delay)
            if not isinstance(got, list) or len(got) != len(part):
                raise indexer.IndexVectorError("Local embedder returned a vector count different from the input.")
            for vector in got:
                indexer.validate_vector(vector, before["dimensions"])
            vectors += got
    finally:
        conn.close()
    after = _probe(base, _left(deadline))
    if (after["model"], after.get("sha256")) != (before["model"], before.get("sha256")):
        raise indexer.IndexCompatibilityError("The local embedder changed models during the call; try again.")
    return vectors
