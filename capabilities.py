#!/usr/bin/env python3
"""Probe de capacidades do Smart Tool (grafo/embedding/rerank), rodado no boot do daemon.

Grafo: client-pure, mesmo contrato do Brain Server (`claude-code-boss/.../graph/daemon.js`) —
lê o registry auto-anunciado em `~/.mcp-memory/run/daemon.json` e faz health-check em
`{url}/health` (200 ou 503 = viva). Nunca spawna o daemon do grafo; se ausente/offline, falha
aberto (graph=False), sem lançar exceção.
"""
import json
import os
import urllib.error
import urllib.request

import model_client

BRAIN_RUN_DIR = os.environ.get("MCP_RUN_DIR") or os.path.join(
    os.path.expanduser("~"), ".mcp-memory", "run"
)
BRAIN_REGISTRY_PATH = os.path.join(BRAIN_RUN_DIR, "daemon.json")


class Capabilities:
    def __init__(self, graph, embed, rerank):
        self.graph = graph
        self.embed = embed
        self.rerank = rerank

    def to_dict(self):
        return {"graph": self.graph, "embed": self.embed, "rerank": self.rerank}


def _probe_graph(timeout=2):
    try:
        with open(BRAIN_REGISTRY_PATH, "r", encoding="utf-8") as f:
            info = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    url = info.get("url") if isinstance(info, dict) else None
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            return resp.status in (200, 503)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _probe_embed_rerank():
    token = model_client.get_token()
    data = model_client.fetch("/v1/models", token)
    ids = [row.get("id", "") for row in data.get("data", [])]
    embed = any("embed" in i.lower() for i in ids)
    rerank = any("rerank" in i.lower() for i in ids)
    return embed, rerank


def probe():
    graph = _probe_graph()
    try:
        embed, rerank = _probe_embed_rerank()
    except Exception:
        embed, rerank = False, False
    return Capabilities(graph, embed, rerank)
