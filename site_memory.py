#!/usr/bin/env python3
"""Sinais diários de sites pesquisados: tópico, domínio, título, URL e trecho.

O daemon usa similaridade (embedding + rerank) para priorizar domínios nas
buscas frescas. O conteúdo bruto não é reutilizado nas respostas MCP sem um
gate de atualidade; esse papel pertence a `research_cache.py`/`freshness.py`.
Design em `design/planos/smart-tool-agente-pesquisa-plano.md`.

Reset diário por comparação de data, não por reinício do processo: o daemon roda como
serviço de longa duração (autostart), então só reiniciar o processo não cobre "resetar por
dia". Sem persistência entre dias e sem ligação com `capture_lesson` — o Smart Tool é MCP
standalone (não depende do plugin que expõe essa tool), e quem tem contexto de sessão pra
decidir se algo virou lição é o agente principal, não o pesquisador aqui dentro, que só
recebe o tópico.
"""
import threading
import time
from datetime import date

# Toda leitura/escrita passa por este lock: o daemon é multi-thread, e chamadas
# concorrentes de web_search mutam o mesmo dict.
_LOCK = threading.Lock()
_daily = {"day": None, "records": []}  # records: [{"topic","domain","tier","title","url","snippet","ts"}]


def _today():
    return date.today().isoformat()


def _ensure_today_locked():
    # Chamado sempre dentro de _LOCK.
    today = _today()
    if _daily["day"] != today:
        _daily["day"] = today
        _daily["records"] = []


def record(topic, domain, tier, title="", url="", snippet=""):
    if not domain:
        return
    with _LOCK:
        _ensure_today_locked()
        _daily["records"].append({
            "topic": topic, "domain": domain, "tier": tier,
            "title": title, "url": url, "snippet": snippet, "ts": time.time(),
        })


def snapshot():
    """Cópia das entradas do dia de hoje — quem chama decide como usar (embed+rerank)."""
    with _LOCK:
        _ensure_today_locked()
        return list(_daily["records"])
