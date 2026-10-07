#!/usr/bin/env python3
"""Registro único dos adaptadores de motor de busca do `web_search`. Cada adaptador é um
subprocesso independente (executável + script) que recebe `[query, "--num", n, *extra_args]`
e devolve `[{title, url, snippet}, ...]` em JSON no stdout — mesmo contrato pra todos, então
`smart_tool_daemon._handle_web_search` itera a lista sem precisar saber o que cada um faz por
dentro. Acrescentar/remover/reordenar um motor é só editar `ADAPTERS`; `router.py` monta o
prompt/schema do modelo de roteamento a partir do campo `hint` de cada entrada, então também
não precisa mudar por adaptador novo.

Três etapas, marcadas por `fan_out` e `fallback_group` (ver ADR 0006):

- `fan_out=True` ("duckduckgo", "open-websearch", "wikipedia", "yahoo", "exa", "parallel", "keenable",
  "google"): baratos e rápidos — `_handle_web_search` dispara todos em paralelo (google pelo navegador
  residente). exa/parallel/keenable seguem o padrão de Hermes e OpenCode: plano público sem chave, chave
  opcional do usuário no mesmo endpoint. e combina os resultados
  de quem responder a tempo, pra garantir diversidade de fonte em toda chamada em vez de
  parar no primeiro que responder.
- `fallback_group="http"` ("tavily", "firecrawl"): APIs com cota gratuita sem chave
  e chave pessoal opcional; entram sob demanda se a cobertura ainda for insuficiente.
- `fallback_group="browser"` ("camoufox", "startpage"): navegador real, mais lento;
  entra só quando as fontes anteriores ainda forem insuficientes, em sequência.
- `service_engine`: o tier roda no navegador Camoufox residente (browser_service.py), uma
  instância com abas reaproveitadas para todos os consumidores, em vez de um script por chamada.

Sem entrada pra Bing: por períodos ele devolve um SERP genérico da entidade (ou de outro
assunto), sempre com 200 e sem erro. Em 2026-10-02 um cliente HTTP com TLS de navegador
recebeu resultados reais (44 de 50); em 2026-10-03 o mesmo cliente, o Camoufox e o Chromium
receberam a isca. Por não lançar erro, mascararia qualquer fallback depois dele; o índice do
Bing entra pelo tier yahoo.
"""
import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_ADAPTER_DIR = os.path.join(PACKAGE_DIR, "web_adapters", "node")
BROWSER_ADAPTER_DIR = os.path.join(PACKAGE_DIR, "web_adapters", "browser")
LOCAL_NODE_EXECUTABLE = os.path.join(NODE_ADAPTER_DIR, ".node-runtime", "node.exe")
NODE_EXECUTABLE = LOCAL_NODE_EXECUTABLE if os.path.isfile(LOCAL_NODE_EXECUTABLE) else "node"
CAMOUFOX_VENV_PYTHON = os.path.join(BROWSER_ADAPTER_DIR, ".venv", "Scripts", "python.exe")
BROWSER_SERVICE_SCRIPT = os.path.join(BROWSER_ADAPTER_DIR, "browser_service.py")
DDG_SEARCH_SCRIPT = os.path.join(NODE_ADAPTER_DIR, "ddg-search.mjs")
OPEN_WEBSEARCH_ADAPTER_SCRIPT = os.path.join(NODE_ADAPTER_DIR, "open-websearch-adapter.mjs")
WIKIPEDIA_SEARCH_SCRIPT = os.path.join(NODE_ADAPTER_DIR, "wikipedia-search.mjs")
EXTERNAL_SEARCH_SCRIPT = os.path.join(PACKAGE_DIR, "web_search_external.py")
DDGS_SEARCH_SCRIPT = os.path.join(PACKAGE_DIR, "web_search_ddgs.py")

FETCH_TIMEOUT_S = 15
OPEN_WEBSEARCH_TIMEOUT_S = 30
WIKIPEDIA_TIMEOUT_S = 15
EXTERNAL_TIMEOUT_S = 18
DDGS_TIMEOUT_S = 15
GOOGLE_TIMEOUT_S = 12
BROWSER_TIMEOUT_S = 60
# O desafio do Startpage continua caro: duas buscas reais isoladas em 2026-09-29
# retornaram resultados em 101s e 154s, mesmo com o cookie curto da primeira ainda
# válido na segunda. Nenhum cookie semanal do Anubis foi emitido; o pré-aquecimento
# periódico foi removido porque não evitava o custo da busca seguinte. Este tier só
# roda como último fallback quando as fontes anteriores não cobrem a consulta.
STARTPAGE_TIMEOUT_S = 240

ADAPTERS = [
    {
        "name": "duckduckgo",
        "executable": NODE_EXECUTABLE,
        "script": DDG_SEARCH_SCRIPT,
        "extra_args": [],
        "timeout_s": FETCH_TIMEOUT_S,
        "fan_out": True,
        # Em inglês (não PT-BR): entra literal no system prompt do router_model em
        # router.py, que é contrato com o modelo — ver convenção de idioma do AGENTS.md.
        "hint": "plain fetch against DuckDuckGo HTML — fastest and cheapest, but fails "
                "when DDG serves its JS anti-bot challenge (fetch never executes JS)",
    },
    {
        "name": "open-websearch",
        "executable": NODE_EXECUTABLE,
        "script": OPEN_WEBSEARCH_ADAPTER_SCRIPT,
        "extra_args": ["--engine", "duckduckgo"],
        "timeout_s": OPEN_WEBSEARCH_TIMEOUT_S,
        "fan_out": True,
        "hint": "same DuckDuckGo index, via the open-websearch npm package's own HTTP "
                "client — no browser, cheaper than camoufox; different access "
                "technique than the plain-fetch duckduckgo tier",
    },
    {
        "name": "wikipedia",
        "executable": NODE_EXECUTABLE,
        "script": WIKIPEDIA_SEARCH_SCRIPT,
        "extra_args": [],
        "timeout_s": WIKIPEDIA_TIMEOUT_S,
        "fan_out": True,
        "hint": "official Wikipedia search API, not a search-engine scrape — genuinely "
                "different index (encyclopedic), no anti-bot risk at all, but narrow: "
                "useless for time-sensitive queries (prices, news, weather)",
    },
    {
        "name": "yahoo",
        "executable": sys.executable,
        "script": DDGS_SEARCH_SCRIPT,
        "extra_args": ["--backend", "yahoo"],
        "timeout_s": DDGS_TIMEOUT_S,
        "fan_out": True,
        "hint": "Yahoo SERP (Bing index) via the ddgs library — a different index than the "
                "DuckDuckGo tiers, ~1 s, keyless; intermittently returns nothing",
    },
    {
        "name": "exa",
        "executable": sys.executable,
        "script": EXTERNAL_SEARCH_SCRIPT,
        "extra_args": ["--provider", "exa"],
        "timeout_s": EXTERNAL_TIMEOUT_S,
        "fan_out": True,
        "hint": "Exa neural index via its public MCP (keyless free tier, optional user key) — strong on official docs and primary sources",
    },
    {
        "name": "parallel",
        "executable": sys.executable,
        "script": EXTERNAL_SEARCH_SCRIPT,
        "extra_args": ["--provider", "parallel"],
        "timeout_s": EXTERNAL_TIMEOUT_S,
        "fan_out": True,
        "hint": "Parallel index via its public search MCP (keyless free tier, optional user key) — ranked excerpts, ~1 s",
    },
    {
        "name": "keenable",
        "executable": sys.executable,
        "script": EXTERNAL_SEARCH_SCRIPT,
        "extra_args": ["--provider", "keenable"],
        "timeout_s": EXTERNAL_TIMEOUT_S,
        "fan_out": True,
        "hint": "Keenable independent index (own crawler and ranking) via its public endpoint, optional user key — ~0.3 s",
    },
    {
        "name": "google",
        "service_engine": "google",
        "timeout_s": GOOGLE_TIMEOUT_S,
        "fan_out": True,
        "hint": "Google SERP via the resident Camoufox browser — best index, ~1-2 s once the "
                "browser is open; one search at a time, paused for an hour after a captcha",
    },
    {
        "name": "tavily",
        "executable": sys.executable,
        "script": EXTERNAL_SEARCH_SCRIPT,
        "extra_args": ["--provider", "tavily"],
        "timeout_s": EXTERNAL_TIMEOUT_S,
        "fan_out": False,
        "fallback_group": "http",
        "hint": "independent ranked web search via Tavily; keyless quota is rate-limited, "
                "so use only when existing results are incomplete or irrelevant",
    },
    {
        "name": "firecrawl",
        "executable": sys.executable,
        "script": EXTERNAL_SEARCH_SCRIPT,
        "extra_args": ["--provider", "firecrawl"],
        "timeout_s": EXTERNAL_TIMEOUT_S,
        "fan_out": False,
        "fallback_group": "http",
        "hint": "independent Firecrawl search API with clean snippets; keyless quota is "
                "rate-limited, so use only when cheaper results remain insufficient",
    },
    {
        "name": "camoufox",
        "service_engine": "duckduckgo",
        "timeout_s": BROWSER_TIMEOUT_S,
        "fan_out": False,
        "fallback_group": "browser",
        "hint": "same DuckDuckGo index, via the resident Camoufox browser — solves the "
                "anti-bot challenge the duckduckgo and open-websearch tiers can't",
    },
    {
        "name": "startpage",
        "service_engine": "startpage",
        "timeout_s": STARTPAGE_TIMEOUT_S,
        "fan_out": False,
        "fallback_group": "browser",
        "hint": "different index (Startpage, via the resident Camoufox browser) — the only fallback "
                "that survives DuckDuckGo itself being down, not just the access "
                "technique; subject to burst rate-limiting",
    },
]

# Merge order of the round-robin. Measured 2026-10-03 (docs/spikes/merge_order_spike.py, 40 real queries, same
# collected results): this order beat the previous random order 16 x 5 (19 ties), SearXNG consensus scoring 14 x 13.
MERGE_PRIORITY = ["exa", "parallel", "google", "tavily", "firecrawl", "camoufox", "startpage", "keenable", "yahoo",
                  "duckduckgo", "open-websearch", "wikipedia", "memoria"]
DEFAULT_TIER_ORDER = [a["name"] for a in ADAPTERS]
SPECS = {a["name"]: a for a in ADAPTERS}
FAN_OUT_TIERS = [a["name"] for a in ADAPTERS if a["fan_out"]]
FALLBACK_TIERS = [a["name"] for a in ADAPTERS if not a["fan_out"]]
HTTP_FALLBACK_TIERS = [a["name"] for a in ADAPTERS if a.get("fallback_group") == "http"]
BROWSER_FALLBACK_TIERS = [a["name"] for a in ADAPTERS if a.get("fallback_group") == "browser"]
