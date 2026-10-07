#!/usr/bin/env python3
"""Catálogo de modelos do gateway para `configure.py` e a tela de setup do Smart Tool.

`/v1/model/info` (proxy LiteLLM) traz o `mode` de cada modelo; um gateway OpenAI padrão só tem `/v1/models`, e aí o
modo sai do nome (embed, rerank; o resto é chat). A lista é sugestão: a tela aceita qualquer id.
"""
import re
import threading
import urllib.error

import gateway
import model_client

_LIGHTWEIGHT_KEYWORDS = ("mini", "nano", "flash", "lite", "haiku", "small")
_LIGHTWEIGHT_RE = re.compile(
    r"\b(?:" + "|".join(_LIGHTWEIGHT_KEYWORDS) + r")\b"
)

_cache_lock = threading.Lock()
_cached_rows = None
_cached_selection_id = None


def is_lightweight(model_id):
    """Heurística por palavra completa (o catálogo não tem campo de tier) — cobre os provedores sem hardcode.
    \\b evita falso positivo tipo "gemini" casando a substring "mini"."""
    model_id = (model_id or "").lower()
    return bool(_LIGHTWEIGHT_RE.search(model_id))


def _fetch_rows():
    """`/v1/model/info` devolve chat+embedding+rerank misturados no mesmo payload --
    cacheado pra vida do processo. Lock só em volta do preenchimento: leitura depois de
    populado é sempre segura, sem necessidade de guarda."""
    global _cached_rows, _cached_selection_id
    selected = gateway.fingerprint()
    if _cached_rows is not None and selected == _cached_selection_id:
        return _cached_rows
    with _cache_lock:
        if _cached_rows is None or selected != _cached_selection_id:
            token = model_client.get_token()
            try:
                rows = model_client.fetch("/v1/model/info", token).get("data", [])
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code not in (400, 404, 405):
                    raise
                rows = [{"model_name": row.get("id"), "model_info": {"mode": _mode_from_name(row.get("id") or "")}}
                        for row in model_client.fetch("/v1/models", token).get("data", [])]
            _cached_rows = rows
            _cached_selection_id = selected
    return _cached_rows


def _mode_from_name(model_id):
    name = model_id.lower()
    return "rerank" if "rerank" in name else "embedding" if "embed" in name else "chat"


def list_models(mode="chat"):
    """Ids de modelo disponíveis para a key atual num dado `mode`
    (`chat`, `embedding` ou `rerank`), deduplicados e ordenados por nome."""
    rows = _fetch_rows()

    names = set()
    for row in rows:
        info = row.get("model_info") or {}
        if info.get("mode") != mode:
            continue
        name = row.get("model_name")
        if name:
            names.add(name)

    return sorted(names)
