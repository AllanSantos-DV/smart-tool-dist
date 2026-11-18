"""Client of the embeddings home (casa-de-embeddings): the per-machine daemon that serves the embedder and forwards
rerank to the host the user configured in it.

The home is found through its announce file. Its embedder is identified by the `model_sha256` and `dimension` of
`/info`, never by a model name. Measured in 2026-10-05 on the rerank gold set (42 code targets, no rerank,
docs/spikes/local_embedder_spike.py): granite-embedding-107m-multilingual on this client 42/35/41 (in pool / 1st /
top 5), MRR@5 0.892; the gateway's text-embedding-3-small 42/35/40, 0.885; lexical only 38/29/36, 0.767.
"""
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse

import config
import indexer
import model_breaker
import model_health

PREFIX = "local:"
ANNOUNCE = os.path.join(os.path.expanduser("~"), ".casa-de-embeddings", "run", "daemon.json")
MIN_API_VERSION = 1
BATCH = 16
INFO_TTL_S = 60
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_S = 60
RERANK_TIMEOUT_S = 15
_lock = threading.Lock()
_info = {"at": 0.0, "value": None, "error": None}


class IdentityError(ValueError):
    """The home is announced or answers, but cannot be used as it is: a malformed announce file, an API version older
    than this client needs, or an `/info` without `model_sha256` or `dimension`."""


def url():
    """Base URL of the home from its announce file; None when the file does not exist (the home is not installed or
    not running). A file that exists but cannot be read as an announce raises IdentityError. localhost becomes
    127.0.0.1: on Windows ::1 is tried first and a daemon bound to IPv4 only costs about 2 s per connection."""
    try:
        with open(ANNOUNCE, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise IdentityError(f"The embeddings home announce file {ANNOUNCE} cannot be read ({exc}).") from None
    base = str(data.get("url") or "").strip().rstrip("/") if isinstance(data, dict) else ""
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise IdentityError(f"The embeddings home announce file {ANNOUNCE} has no http(s) url.")
    version = data.get("apiVersion")
    if type(version) is not int or version < MIN_API_VERSION:
        raise IdentityError(f"The embeddings home announces API version {version!r}; Smart Tool needs "
                            f"{MIN_API_VERSION} or later. Update the embeddings home.")
    if parsed.hostname == "localhost":
        parsed = parsed._replace(netloc=parsed.netloc.replace("localhost", "127.0.0.1", 1))
    return urllib.parse.urlunsplit(parsed)


def configured():
    """Whether an announce file of the home exists (malformed counts: it raises at use, loudly)."""
    return os.path.isfile(ANNOUNCE)


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
        detail = data[:300].decode("utf-8", "replace").strip()
        raise urllib.error.HTTPError(f"{conn.host}:{conn.port}{path}", response.status,
                                     f"{response.reason}{': ' + detail if detail else ''}", response.headers, None)
    return json.loads(data)


def _probe(base, timeout):
    conn = _connection(base, timeout)
    try:
        data = _request(conn, "GET", "/info")
    finally:
        conn.close()
    digest = str(data.get("model_sha256") or "").strip().lower()
    try:
        dimensions = int(data.get("dimension") or 0)
    except (TypeError, ValueError):
        dimensions = 0
    if not digest or dimensions <= 0:
        raise IdentityError(f"The embeddings home at {base} answered /info without model_sha256 or dimension, so its "
                            "model cannot be identified; Smart Tool will not fall back to lexical search silently. "
                            "Update the embeddings home so /info reports model_sha256 and dimension.")
    cpu = bool(data.get("demoted_to_cpu")) or str(data.get("provider") or "").startswith("CPU")
    rerank_host = data.get("rerank_host") if isinstance(data.get("rerank_host"), dict) else {}
    return {"model": f"{PREFIX}{digest[:16]}-{dimensions}", "name": data.get("model_id") or "", "dimensions": dimensions,
            "url": base, "sha256": digest, "cpu": cpu, "rerank": rerank_host.get("configured") is True}


def runs_on_cpu(model):
    """Whether `model` is the home's embedder and the home computes on the CPU (no GPU, or demoted to the CPU)."""
    return is_local(model) and bool((info() or {}).get("cpu"))


def info(timeout=1.5):
    """Home /info cached for INFO_TTL_S; None when it is not announced or not answering. A home that is announced or
    answers but cannot be used raises IdentityError. The probe runs under the lock so a slow failing probe never
    overwrites a newer good answer."""
    with _lock:
        if time.monotonic() - _info["at"] >= INFO_TTL_S:
            value, error = None, None
            try:
                base = url()
                if base:
                    value = _probe(base, timeout)
            except IdentityError as exc:
                error = exc
            except (OSError, ValueError, http.client.HTTPException):
                pass
            _info.update(at=time.monotonic(), value=value, error=error)
        if _info["error"]:
            raise _info["error"]
        return _info["value"]


def is_local(model):
    return bool(model) and model.startswith(PREFIX)


def resolve(cfg=None, current=None):
    """Embedding model the index of a folder must be built with, following config `embedder`.

    `gateway` is the user's explicit choice: the gateway's `embedding_model`, or lexical without one. `home` (the
    default) is the home's embedder. While the home is not running, an index the home can keep (its own, or lexical)
    is kept as it is, so a restart of the home never triggers a rebuild to lexical and back; an index of another model
    gives None: it keeps answering and is not updated, and re-embedding waits for the home. A folder without an index
    is indexed lexically meanwhile. The gateway is never used in place of the home."""
    cfg = cfg if cfg is not None else config.load_config()
    if config.embedder(cfg) == "gateway":
        return cfg.get("embedding_model") or indexer.LEXICAL_MODEL
    available = info()
    if available:
        return available["model"]
    if not current or is_local(current) or current == indexer.LEXICAL_MODEL:
        return current or indexer.LEXICAL_MODEL
    return None


def rerank_configured():
    """Whether the home reports a rerank host configured in it."""
    return bool((info() or {}).get("rerank"))


def rerank(query, documents, top_n, deadline=None):
    """The home's `POST /rerank` answer in the canonical form `{results: [{index, relevance_score}]}`. The home forwards
    to the host the user configured; an error answer raises, it is never replaced by another reranker."""
    base = url()
    if not base:
        raise RuntimeError(f"The embeddings home is not running (no announce file at {ANNOUNCE}).")
    timeout = RERANK_TIMEOUT_S if deadline is None else min(RERANK_TIMEOUT_S, _left(deadline))
    conn = _connection(base, timeout)
    try:
        return _request(conn, "POST", "/rerank", {"query": query, "documents": documents, "top_n": top_n})
    finally:
        conn.close()


def _left(deadline):
    if deadline is None:
        return REQUEST_TIMEOUT_S
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Deadline exceeded before local embeddings finished.")
    return min(REQUEST_TIMEOUT_S, left)


def _retry_delay(failure, attempt):
    """Seconds to wait after a failed attempt: the `Retry-After` of the answer (whole seconds, at most the pause of the
    circuit) when the home sent one, else the attempt number."""
    value = str(getattr(failure, "headers", None) and failure.headers.get("Retry-After") or "")
    return min(int(value), model_breaker.PAUSE_S) if value.isdigit() else attempt


def embed(model, texts, deadline=None, cancel_check=None, on_retry=None):
    base = url()
    if not base:
        raise RuntimeError(f"The embeddings home is not running (no announce file at {ANNOUNCE}).")
    # Fresh /info before and after: /embed does not echo the model, and a cached answer could let vectors of a
    # different model reach the persistent vector cache under this model's contract.
    try:
        before = _probe(base, _left(deadline))
    except IdentityError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise RuntimeError(f"The embeddings home is not responding ({base}): {exc}") from None
    if before["model"] != model:
        raise indexer.IndexCompatibilityError(
            f"The embeddings home now serves {before['model']}; the index was built with {model} and must be rebuilt.")
    vectors = []
    conn = _connection(base, _left(deadline))
    try:
        for start in range(0, len(texts), BATCH):
            part = texts[start:start + BATCH]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if cancel_check:
                    cancel_check()
                model_breaker.guard(model)
                conn.timeout = _left(deadline)
                try:
                    with model_breaker.slot(model):
                        got = _request(conn, "POST", "/embed", {"inputs": part})
                except (OSError, http.client.HTTPException) as exc:
                    model_breaker.note(model, exc)
                    model_health.record("/v1/embeddings", {"model": model}, exc)
                    conn.close()
                    conn = _connection(base, _left(deadline))
                    if attempt == MAX_ATTEMPTS:
                        raise RuntimeError(f"Embeddings home failed after {MAX_ATTEMPTS} attempts: {exc}") from None
                    if on_retry:
                        on_retry(attempt + 1, len(part))
                    delay = _retry_delay(exc, attempt)
                    if deadline is not None and time.monotonic() + delay >= deadline:
                        raise TimeoutError("Not enough time left to retry the embeddings home.") from None
                    time.sleep(delay)
                else:
                    model_breaker.note(model)
                    model_health.record("/v1/embeddings", {"model": model})
                    break
            if not isinstance(got, list) or len(got) != len(part):
                raise indexer.IndexVectorError("The embeddings home returned a vector count different from the input.")
            for vector in got:
                indexer.validate_vector(vector, before["dimensions"])
            vectors += got
    finally:
        conn.close()
    after = _probe(base, _left(deadline))
    if (after["model"], after.get("sha256")) != (before["model"], before.get("sha256")):
        raise indexer.IndexCompatibilityError("The embeddings home changed models during the call; try again.")
    return vectors
