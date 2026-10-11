#!/usr/bin/env python3
"""Daemon HTTP único e persistente do Smart Tool — expõe `smart_search` via MCP
Streamable HTTP (JSON-RPC 2.0 sobre POST, respostas síncronas em JSON puro, sem exigir
SSE). Auto-anunciado em `~/.smart-tool/data/daemon.json`, no mesmo contrato de
registry do Brain Server (`{"url": ...}`), pra `daemon_launcher.ensure_daemon_running()`
health-checar e reusar entre sessões/hosts.

Modelos pelo adaptador do gateway configurado: `embeddings` (OpenAI-style, `{"model", "input"}` →
`data[].embedding`) e `rerank` (Cohere-style, `{"model", "query", "documents", "top_n"}` → `results[].index`).
"""
import atexit
import concurrent.futures
import contextlib
import contextvars
import itertools
import datetime
import email.utils
import hashlib
import json
import math
import os
import paths
import re
import random
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_control
import capabilities
import client_hooks
import install_runtime
import integration_metrics
import config
import freshness
import model_client
import index_scope
import index_profile
import indexer
import index_views
import index_inventory
import usage_meter
import document_text
import embedding_cache
import model_defaults
import project_identity
import project_store
import project_scheduler
import project_monitor
import research_metrics
import research_cache
import router
import setup_ui
import site_memory
import web_search_adapters
import browser_service_client
import local_embedder
import web_search_health
import web_fetch
import version
import gateway
import hook_decision
import endpoint_sync

RUN_DIR = paths.DATA_DIR
REGISTRY_PATH = os.path.join(RUN_DIR, "daemon.json")
DEFAULT_PORT = int(os.environ.get("SMART_TOOL_PORT", "8765"))
PROTOCOL_VERSION = "2025-06-18"
MCP_MAX_REQUEST_BYTES = 10 * 1024 * 1024
_BOUND_PORT = DEFAULT_PORT

DDG_SEARCH_SCRIPT = web_search_adapters.DDG_SEARCH_SCRIPT
CAMOUFOX_VENV_PYTHON = web_search_adapters.CAMOUFOX_VENV_PYTHON
CRAWL4AI_SEARCH_SCRIPT = os.path.join(web_search_adapters.BROWSER_ADAPTER_DIR, "crawl4ai_search.py")
WEB_SEARCH_TIER1_TIMEOUT_S = web_search_adapters.FETCH_TIMEOUT_S
WEB_SEARCH_MAX_QUERY_CHARS = 500
SEARCH_SCRIPT_ERROR_MAX_CHARS = 500
_IS_WINDOWS = sys.platform == "win32"
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SITE_OPERATOR_RE = re.compile(r"(?<!\S)site:([a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9])", re.IGNORECASE)

DEEP_JOB_WORKERS = 2
DEEP_JOB_MAX_PAGES = 5
DEEP_JOB_TIMEOUT_S = 180
DEEP_JOB_SYNTHESIS_MAX_CHARS = 100_000
DEEP_JOB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=DEEP_JOB_WORKERS, thread_name_prefix="deep-job"
)

# RESEARCH_MAX_ITERATIONS é configurável (config.research_max_iterations, recomendado e
# validado em design/planos/smart-tool-agente-pesquisa-item1-integracao.md) — lido por
# rodada em _run_research_job, não fixo aqui. Teto de wall-clock segue o mesmo papel de
# DEEP_JOB_TIMEOUT_S pro Tier 4.
RESEARCH_JOB_TIMEOUT_S = 180
RESEARCH_CACHE_CHECK_S = 25
RESEARCH_LLM_RESERVE_S = 90
# Duas chamadas reais em 2026-09-29 esgotaram 30s antes de o job usar 40s.
# Cada chamada pode esperar até 60s, sempre limitada pelo prazo total acima.
RESEARCH_LLM_TIMEOUT_S = 60
RESEARCH_JOB_WORKERS = 2
RESEARCH_JOB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=RESEARCH_JOB_WORKERS, thread_name_prefix="research-job"
)

WEB_CACHE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="web-cache"
)
WEB_CACHE_CLASSIFY_TIMEOUT_S = 30
WEB_CACHE_PENDING_GRACE_S = 120
WEB_LANG_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="web-lang"
)

WEB_SEARCH_DEFAULT_TIER_ORDER = web_search_adapters.DEFAULT_TIER_ORDER
# Sem navegador (ver web_search_adapters.FAN_OUT_TIERS): baratos o bastante pra disparar
# todos em paralelo em toda chamada, garantindo fonte diversa em vez de parar no primeiro
# que responder. 20s cobre as quatro com folga mesmo sob rede lenta (nenhuma passa de
# alguns segundos quando não bloqueada) sem herdar o orçamento dos tiers de navegador.
WEB_SEARCH_FAN_OUT_BUDGET_S = 20
# ~5 concurrent fan-outs (a bilingual quick search runs two) before tiers queue inside the budget.
WEB_SEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=6 * len(web_search_adapters.FAN_OUT_TIERS), thread_name_prefix="web-search"
)
WEB_PROVIDER_CIRCUIT_DEFAULT_S = 300
WEB_PROVIDER_CIRCUIT_MIN_S = 30
WEB_PROVIDER_CIRCUIT_MAX_S = 3600
_web_provider_circuits = {}
_web_provider_circuit_lock = threading.Lock()


class WebProviderRateLimited(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = min(WEB_PROVIDER_CIRCUIT_MAX_S, max(1, int(retry_after)))
        super().__init__(f"HTTP 429; retrying in {self.retry_after}s")

# SITE_MEMORY_SIMILARITY_THRESHOLD é configurável (config.site_memory_similarity_threshold,
# recomendado a partir do spike 2: 0.42 de similaridade pra tópico próximo vs. 0.26/0.20 pra
# distante, com `text-embedding-3-small`) — lido em _site_memory_correlate, não fixo
# aqui.
SITE_MEMORY_MAX_TOPICS = 50
# Em pares reais de 2026-09-29, o reranker marcou falsos positivos de cosine
# em até 0,064 e pares úteis a partir de 0,148. 0,12 separa as duas amostras
# sem endurecer o limiar de embedding (0,35), que é só filtro de candidatos.
SITE_MEMORY_RERANK_MIN_SCORE = 0.12


def _parse_bounded_int(value, default, min_v, max_v, field_name):
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{field_name}' must be an integer, got: {value!r}")
    if n < min_v or n > max_v:
        raise ValueError(f"'{field_name}' must be between {min_v} and {max_v}, got: {n}")
    return n

_sessions = {}
_capabilities_cache = None

_jobs = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS_RETAINED = 200
SMART_SEARCH_SYNC_WAIT_S = 3
# smart_search_result waits for the job this long before answering pending: answering at once had an agent poll the
# same job 7 times in 16 s and then give up on Smart Tool (claude -p session, 2026-10-09; job durations median 6 s,
# p75 10.7 s).
SMART_SEARCH_RESULT_WAIT_S = 15
SMART_SEARCH_MAX_PENDING = 8
SMART_SEARCH_EXECUTOR = project_scheduler.ProjectScheduler(workers=3)
_PROJECT_LOCKS = {}
_PROJECT_LOCKS_LOCK = threading.Lock()
_PROJECT_MONITOR = None
_PROJECT_ACTION_LOCK = threading.RLock()
_JOB_FUTURES = {}
_PERSIST_LAST = {}
_PROJECT_KINDS = {"smart_search", "project_index", "project_preview"}
_GATEWAY_STATUS = {}
_GATEWAY_LOCK = threading.Lock()
INDEX_JOB_TIMEOUT_S = 1800


class ProjectJobCancelled(RuntimeError):
    pass


def _persist_project_job(job_id, job):
    if job.get("kind") in _PROJECT_KINDS:
        project_store.save_job(job_id, job)
        usage_meter.record_job(job_id,job)


def _set_job(job_id, **fields):
    # Substituição atômica da entrada inteira (nunca mutação campo a campo, o que
    # deixaria uma leitura concorrente ver ex. status="done" com result ainda
    # None) — a poda de jobs antigos entra no mesmo lock pra nunca iterar o dict
    # enquanto outra thread insere, o que rodaria RuntimeError no CPython.
    with _JOBS_LOCK:
        fields = {**_jobs.get(job_id, {}), **fields, "updated_at": time.time()}
        if fields.get("status") != "pending":
            fields["finished_at"] = time.time()
        _jobs[job_id] = fields
        if len(_jobs) > _MAX_JOBS_RETAINED:
            # Nunca podar jobs "pending": um worker que ainda vai chamar
            # _set_job de novo (sucesso ou erro) recriaria a entrada removida
            # sem checar se já existia — o cliente veria "job_id desconhecido"
            # num job que na verdade ainda está rodando/na fila.
            prunable = sorted(
                (k for k, v in _jobs.items() if v.get("status") != "pending"),
                key=lambda k: _jobs[k].get("created_at", 0),
            )
            for old_id in prunable[: len(_jobs) - _MAX_JOBS_RETAINED]:
                _jobs.pop(old_id, None)
        _persist_project_job(job_id, fields)


def _update_job(job_id, **fields):
    if fields.get('phase'):usage_meter.phase(fields['phase'])
    persistent = None
    with _JOBS_LOCK:
        current = _jobs.get(job_id)
        if current is not None and current.get("status") == "pending":
            updated = {**current, **fields, "updated_at": time.time()}
            _jobs[job_id] = updated
            if (fields.get("phase") != current.get("phase") or
                    time.monotonic() - _PERSIST_LAST.get(job_id, 0) > .5 or fields.get("cancel_requested")):
                _PERSIST_LAST[job_id] = time.monotonic()
                persistent = updated
        if persistent:
            _persist_project_job(job_id, persistent)


def _project_lock(root):
    key = project_identity.canonical_root(root)
    with _PROJECT_LOCKS_LOCK:
        return _PROJECT_LOCKS.setdefault(key, threading.Lock())


_SMART_SEARCH_PHASES = {
    "queued": "Search queued for indexing.",
    "waiting_project": "Waiting for another search in this project to finish.",
    "auth": "Checking the model gateway.",
    "scope": "Analyzing the project scope.",
    "profile": "Classifying code, docs and languages.",
    "listing": "Listing project files.",
    "indexing": "Indexing project files.",
    "indexing_retry": "Waiting to retry embeddings.",
    "embedding_query": "Preparing the semantic query.",
    "searching": "Searching the index.",
    "reranking": "Ranking results.",
    "model_probe": "Validating the embedding model.",
    "preview": "Checking scope files and rules.",
}


def _smart_search_job_payload(job_id):
    with _JOBS_LOCK:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        job = project_store.job(job_id) or {}
    if job.get("kind") not in _PROJECT_KINDS:
        raise ValueError("Unknown or expired smart_search job_id; run the search again.")
    payload = {"job_id": job_id, "status": job["status"], "kind": job.get("kind"),
               "project_id": job.get("project_id"), "updated_at": job.get("updated_at")}
    if job["status"] == "pending":
        processed = job.get("processed_files")
        total = job.get("total_files")
        phase = job.get("phase", "queued")
        message = _SMART_SEARCH_PHASES.get(phase, "Search in progress.")
        if phase == "indexing" and total is not None:
            message = f"Indexing file {processed or 0} of {total}." if total else "Index up to date; preparing the search."
        elif phase == "indexing_retry":
            message = (f"Embedding batch was slow; attempt {job.get('retry_attempt', 2)} "
                       f"of {EMBED_MAX_ATTEMPTS}. Files done: {processed or 0} of {total or 0}.")
        payload.update({
            "phase": phase,
            "message": message,
            "next_tool": "smart_search_result",
            "processed_files": processed,
            "total_files": total,
            "elapsed_s": round(time.time() - job["created_at"], 1),
            "retry_after_s": 3,
            "cancel_requested": bool(job.get("cancel_requested")),
        })
    elif job["status"] == "done":
        payload["result"] = job["result"]
        if job.get("warning") and job["warning"] not in str(job["result"]):
            payload["warning"] = job["warning"]
    else:
        payload["error"] = job.get("error") or "Search did not complete."
        payload["resumable"] = bool(job.get("arguments"))
    if job.get("stats") and job["stats"] != payload.get("result"):
        payload["stats"] = job["stats"]
    if job.get("failure_phase"):
        payload["failure_phase"] = job["failure_phase"]
    if job.get('view'):
        payload['view'] = job['view']
    return payload

# Claude Code defers MCP tools behind tool search; the tools the hook redirects to (and their job polling) load
# upfront, so a blocked agent already has the replacement in its tool list.
ALWAYS_LOAD = {"anthropic/alwaysLoad": True}
SERVER_INSTRUCTIONS = (
    "Smart Tool serves an indexed semantic search over the user's projects and a cheaper web reader. Use smart_search "
    "FIRST to explore code in the user's projects (where something is implemented, how a flow works, what already "
    "exists to reuse) before reading files one by one; Grep/Read are for an exact string or a file you already know. "
    "Use web_search/web_fetch for any web research. The user also installed "
    "a PreToolUse hook that blocks content searches over a project folder (grep -r, rg, git grep, Grep on a folder) "
    "and native WebFetch/WebSearch. A call blocked by Smart Tool is a routing rule, not a failure: make the call the block "
    "message names (smart_search with project_root and query_identifiers, web_fetch with url and prompt, web_search "
    "with query). Never retry the same search or download through Bash, python, node, PowerShell, curl or wget: that "
    "bypasses the rule the user set. Reading one known file, or searching inside one known file, is never blocked. "
    "Before changing a function, project_manage action=graph with symbol lists its callers and tests; after editing, "
    "project_manage action=affected_tests lists the tests to run."
)

TOOLS = [
    {
        "name": "project_manage",
        "description": "Manages folders and indexing. Every action takes project_root (the folder) or project_id. list gives a short line per project; status shows storage, scope and jobs. graph with symbol (function, Class.method, class name, or path::name when the name repeats) answers what an edit touches: definition, callers with line, calls, tests that reach it through static calls (depth hops, default 3) and files importing it; use it before changing a function to know what to update. affected_tests answers which tests to run after editing: every test file importing a changed file (git diff against base, default HEAD, untracked included) plus changed tests, likeliest failures first, with the command to run them and run_all when config or unanalyzed code changed. graph without symbol reads the indexed view: coverage, references and static calls in Java, Angular, Python and JS/TS/JSX/TSX; includes HTML/CSS/Markdown references and TXT/DOCX coverage; with file_path, narrows to that file's symbols, imports and calls (lists capped by limit). inspect lists the files of a view_id; with file_path, returns only that file's chunks. usage shows consumption and reuse; docs lists public functions without a docstring (Python docstring, JSDoc, Javadoc; private, nested and override functions excluded) with coverage percent, excluding tests unless include_tests=true, for documenting a project that started without it; duplicates lists duplicated functions (identical, near-identical and semantic bodies, min_similarity default 0.90; below that most pairs measured were false positives), excluding tests unless include_tests=true; fix real duplicates; only dismiss with duplicates_dismiss (finding_id, reason=false_positive|intentional, note) what is not a duplicate or is a copy kept on purpose: the finding stays hidden until the code changes, and duplicates_restore reopens it; integration shows the MCP client, hook and tool usage metrics; install_hook_preview shows the change that would install the hook redirecting Grep/Read to smart_search and WebSearch/WebFetch to web_search/web_fetch in this session's client and returns a plan_id; install_hook writes it with that plan_id and confirm=true, with a backup, only after the user approves the change; search_limits saves the project's default top_k per block ({code, test, doc}, each 1-20, total at most 20); storage shows disk usage and views; pin_view protects a view; cleanup_preview and cleanup_commit remove selected views after confirmation; compare compares two stored views, without checkout or embeddings. register adds a folder; preview checks the scope; index/rebuild update; pause/cancel keep checkpoints; resume continues; policy picks on_search/eager; scope adjusts the scope; remove deletes only the local index. Jobs via smart_search_result.",
        "inputSchema": {
            "type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "status", "register", "preview", "index", "rebuild", "pause", "cancel", "resume", "watch", "scope", "remove", "relocate", "probe", "policy", "inspect", "graph", "usage", "storage", "pin_view", "cleanup_preview", "cleanup_commit", "compare", "search_limits", "integration", "install_hook_preview", "install_hook", "duplicates", "duplicates_dismiss", "duplicates_restore", "docs", "affected_tests"]},
                "finding_id": {"type": "string"},
                "reason": {"type": "string", "enum": ["false_positive", "intentional"]},
                "note": {"type": "string"},
                "include_dismissed": {"type": "boolean", "default": False},
                "min_similarity": {"type": "number", "minimum": 0.5, "maximum": 1, "default": 0.9,
                                   "description": "Semantic cutoff for duplicates; keep 0.90: below it most pairs were false positives in measurement"},
                "include_tests": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
                "days": {"type":"integer","minimum":1,"maximum":366},
                "top_k": {"type": "object", "additionalProperties": False, "properties": {
                    "code": {"type": "integer", "minimum": 1, "maximum": 20},
                    "test": {"type": "integer", "minimum": 1, "maximum": 20},
                    "doc": {"type": "integer", "minimum": 1, "maximum": 20}}},
                "storage_id":{"type":"string"},"storage_ids":{"type":"array","items":{"type":"string"}},
                "pinned":{"type":"boolean"},"clear_cache":{"type":"boolean"},"plan_id":{"type":"string"},"confirm":{"type":"boolean"},
                "left_view":{"type":"string"},"right_view":{"type":"string"},"relations":{"type":"boolean"},
                "update_mode": {"type": "string", "enum": ["on_search", "eager"]},
                "view_id": {"type": "string"}, "file_path": {"type": "string"},
                "base": {"type": "string", "default": "HEAD", "description": "affected_tests: git revision the changes are compared with (HEAD = uncommitted work; main or origin/main for a branch)"},
                "symbol": {"type": "string", "description": "graph: function, Class.method, class name or path::name to get its callers, calls, tests and importers"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 3, "description": "graph with symbol: call hops searched for tests"},
                "project_root": {"type": "string"}, "project_id": {"type": "string"},
                "job_id": {"type": "string"}, "watch": {"type": "boolean"},
                "manual": {"type": "boolean"}, "force_scope": {"type": "boolean"},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "user_exclude": {"type": "array", "items": {"type": "string"}}
            }, "required": ["action"], "additionalProperties": False
        }
    },
    {
        "name": "smart_search",
        "_meta": ALWAYS_LOAD,
        "description": (
            "Semantic search (embed+rerank) over the project's index: the first step to understand code in a "
            "project, finding functions, flows and reusable code by meaning, ranked, with name:line. Use it before "
            "a series of Grep/Read; use Grep only for an exact literal or inside a known file. "
            "Results come in three blocks, code, test and doc, plus the project profile with the natural "
            "languages of identifiers, comments and docs (also in project_manage list/status as search_profile). "
            "Fill query_identifiers with identifier and function-name terms in the identifiers' language and "
            "query_comments with the same request in the comments' language; when the profile shows two "
            "languages both are required, or explain in single_language_reason. Put documentation searches in "
            "doc_query. Code and test results list the likeliest functions as name:line in symbols. Tune top_k per block for the project (project_manage search_limits). Fast searches return results directly. If work takes longer than a few seconds, "
            "a pending job_id is returned; call smart_search_result with that ID until done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Absolute path to the project root"},
                "query_identifiers": {"type": "string", "description": "Code and test search written with identifier and function-name terms, in the identifiers' language; required unless group is doc"},
                "query_comments": {"type": "string", "description": "The same search in the other language detected in the project (comments); required whenever the profile lists more than one language"},
                "doc_query": {"type": "string", "description": "Documentation search, in the docs' language; defaults to query_comments"},
                "single_language_reason": {"type": "string", "description": "Only to skip query_comments: at least 15 characters on why one language is right here (e.g. searching literal UI text)"},
                "group": {"type": "string", "enum": ["code", "test", "doc", "all"], "default": "all",
                          "description": "Which blocks to return"},
                "top_k": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 20},
                                    {"type": "object", "additionalProperties": False, "properties": {
                                        "code": {"type": "integer", "minimum": 1, "maximum": 20},
                                        "test": {"type": "integer", "minimum": 1, "maximum": 20},
                                        "doc": {"type": "integer", "minimum": 1, "maximum": 20}}}],
                          "description": "Results per block: one number for all blocks or {code, test, doc}; each 1-20, total at most 20. Defaults to the project's search_limits, else 5"},
            },
            "required": ["project_root"],
        },
    },
    {
        "name": "smart_search_result",
        "_meta": ALWAYS_LOAD,
        "description": (
            "Checks the progress and result of a background smart_search. "
            "Pass the job_id returned by smart_search; while pending, wait retry_after_s and check again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "job_id returned by smart_search"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "web_search",
        "_meta": ALWAYS_LOAD,
        "description": (
            "Web search with curated results (title/url/snippet) - use instead of "
            "the native WebSearch: several sources in parallel, the query in two languages and a cache shared across sessions. Starts with free HTTP "
            "sources; escalates to Tavily, Firecrawl and, if needed, Camoufox/Startpage "
            "only when results are insufficient. Tavily and Firecrawl take "
            "optional API keys in the local setup. Classifies how volatile the findings are "
            "and validates the cache before reusing it; npm versions check the registry and "
            "OSV advisories. With depth='deep', runs an asynchronous "
            "multi-page crawl of ONE site (Tier 4). With depth='research', runs "
            "several search rounds on its own until it decides it has enough on the topic "
            "(both return a job_id immediately - use web_search_result to fetch the "
            "result later). research returns findings, sources, volatility and cache state. "
            "'quick' returns results and, when something degraded (gateway, embedding, rerank, one language), warnings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query, or the topic in natural language when depth='research'"},
                "query_en": {"type": "string", "description": (
                    "Required with depth='quick': the same query written in English (repeat query if it is already "
                    "in English). Both run in parallel: English brings the official docs and the "
                    "original language keeps local references.")},
                "num": {"type": "integer", "description": "How many results to return (or per round, with depth='research')", "default": 8},
                "scope": {
                    "type": "string",
                    "description": "Optional hint about the target (e.g. 'site with bot detection', 'recent news') so the router picks the tier",
                },
                "depth": {
                    "type": "string",
                    "enum": ["quick", "deep", "research"],
                    "description": (
                        "'quick' (default): synchronous search on the fast tiers. 'deep': "
                        "asynchronous multi-page crawl (Tier 4) for the full spec of a "
                        "repository/system/site — always asynchronous, always Tier 4. "
                        "'research': given a broad topic, runs several search rounds "
                        "(configurable cap) deciding on its own when it has enough to "
                        "answer — returns "
                        "structured findings (findings/gaps) + sources, not a finished "
                        "answer (the calling agent writes the final answer)."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "_meta": ALWAYS_LOAD,
        "description": (
            "Reads a known URL and returns only the answer to the prompt, written by the gateway's mini model - use instead of "
            "the native WebFetch. The page is cached for 24 h: another prompt on the same URL does not download it again. "
            "JavaScript-built pages are rendered in the browser. Does not read localhost, internal networks or PDFs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s) URL of the page"},
                "prompt": {"type": "string", "description": "What to extract or answer from the page"},
            },
            "required": ["url", "prompt"],
        },
    },
    {
        "name": "web_search_result",
        "_meta": ALWAYS_LOAD,
        "description": (
            "Checks the status/result of a deep research job "
            "(web_search with depth='deep' or depth='research')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "job_id returned by web_search with depth='deep' or depth='research'"},
            },
            "required": ["job_id"],
        },
    },
]


_UNSAFE_UNICODE_CATEGORIES = ("Cc", "Cf")  # controle (ex.: ESC) e formatação (ex.: RTL override, zero-width)
_SAFE_CONTROL_CHARS = {"\t", "\n", "\r"}  # whitespace comum em texto real multi-linha, não o vetor de injeção visado


def _sanitize_text(text):
    # Neutraliza caractere por caractere (não basta envolver em aspas: um
    # caractere de formatação Unicode como RTL override afeta a renderização no
    # terminal mesmo dentro de uma string JSON válida) — texto vindo de scraping
    # de terceiros, síntese de LLM, ou stderr de subprocesso não é input
    # confiável, e o destino final é um terminal/CLI. Tab/newline/CR ficam de
    # fora do conjunto neutralizado: são whitespace comum, não o vetor de
    # injeção (ESC/ANSI, RTL override) que esta função visa.
    return "".join(
        ch if ch in _SAFE_CONTROL_CHARS or unicodedata.category(ch) not in _UNSAFE_UNICODE_CATEGORIES
        else f"\\u{ord(ch):04x}"
        for ch in str(text)
    )


_YAML_LEADING_INDICATORS = set("[]{}!&*#|>'\"%@`,?:-")  # indicadores reservados no início de um plain scalar YAML


def _yaml_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = _sanitize_text(value)
    needs_quoting = (
        text == "" or text != text.strip() or any(ch in text for ch in ":\n#\"'")
        or text[0] in _YAML_LEADING_INDICATORS
    )
    if needs_quoting:
        return json.dumps(text, ensure_ascii=False)
    return text


def _to_yaml(results):
    if not results:
        return "[]\n"
    lines = []
    for item in results:
        first = True
        for key, value in item.items():
            prefix = "- " if first else "  "
            lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
            first = False
    return "\n".join(lines) + "\n"


def _sanitize_value_deep(value):
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {k: _sanitize_value_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value_deep(v) for v in value]
    return value


def _to_json_sanitized(obj):
    # Sanitiza só os valores string ANTES de serializar — nunca o texto JSON já
    # pronto: json.dumps(indent=2) insere seus próprios caracteres de quebra de
    # linha/indentação, que são categoria Cc igual a um caractere de controle
    # malicioso; sanitizar o texto final trocaria essa formatação legítima por
    # um escape visível, quebrando a estrutura do JSON.
    return json.dumps(_sanitize_value_deep(obj), ensure_ascii=False, indent=2)


EMBED_BATCH_MAX_CHARS = indexer.REINDEX_BATCH_MAX_CHARS
EMBED_BATCH_MAX_ITEMS = indexer.REINDEX_BATCH_MAX_ITEMS
EMBED_REQUEST_TIMEOUT_S = 60
EMBED_MAX_ATTEMPTS = 3


def _embed_batches(texts, max_chars=EMBED_BATCH_MAX_CHARS, max_items=EMBED_BATCH_MAX_ITEMS):
    batch, batch_chars = [], 0
    for text in texts:
        if batch and (batch_chars + len(text) > max_chars or len(batch) >= max_items):
            yield batch
            batch, batch_chars = [], 0
        batch.append(text)
        batch_chars += len(text)
    if batch:
        yield batch


def _transient_embed_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            try:
                detail = exc.read(4096).decode("utf-8", "replace").casefold()
            except Exception:
                detail = ""
            if any(marker in detail for marker in ("insufficient_quota", "budget_exceeded", "budget exceeded", "budget has been exceeded")):
                return False
        return exc.code in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError))


def _embed_retry_delay(exc, attempt):
    value = (getattr(exc, "headers", None) or {}).get("Retry-After")
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (email.utils.parsedate_to_datetime(value) - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                seconds = 0
        if seconds > 30:
            raise RuntimeError(f"The gateway asked to back off for {int(seconds)} s. Resume the job after that.")
        return max(0.1, seconds)
    return min(8, 2 ** (attempt - 1)) + random.uniform(0, .25)


def _embed(model, texts, token, deadline=None, on_retry=None, cancel_check=None):
    if local_embedder.is_local(model):
        return local_embedder.embed(model, texts, deadline=deadline, cancel_check=cancel_check, on_retry=on_retry)
    vectors = []
    dimensions = None
    for batch in _embed_batches(texts):
        max_attempts = EMBED_MAX_ATTEMPTS if deadline is None or on_retry is not None else 1
        for attempt in range(1, max_attempts + 1):
            if cancel_check:
                cancel_check()
            timeout = (min(EMBED_REQUEST_TIMEOUT_S, _research_time_left(deadline))
                       if deadline is not None else EMBED_REQUEST_TIMEOUT_S)
            try:
                response = model_client.fetch(
                    "/v1/embeddings", token, method="POST", body={"model": model, "input": batch},
                    timeout=timeout,
                )
            except Exception as exc:
                if attempt >= max_attempts or not _transient_embed_error(exc):
                    raise
                if on_retry:
                    on_retry(attempt + 1, len(batch))
                delay = _embed_retry_delay(exc, attempt)
                if deadline is not None and delay >= _research_time_left(deadline):
                    raise TimeoutError("Not enough time left to honor the gateway backoff.")
                if cancel_check:
                    until = time.monotonic() + delay
                    while time.monotonic() < until:
                        cancel_check()
                        time.sleep(min(.2, max(0, until - time.monotonic())))
                else:
                    time.sleep(delay)
                continue
            if cancel_check:
                cancel_check()
            rows = response.get("data", []) if isinstance(response, dict) else []
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise RuntimeError("The gateway returned a different number of embeddings than the input.")
            ordered = [None] * len(batch)
            for row in rows:
                if not isinstance(row, dict):
                    raise indexer.IndexVectorError("Invalid embedding row.")
                idx = row.get("index", 0 if len(batch) == 1 else None)
                if type(idx) is not int or not 0 <= idx < len(batch) or ordered[idx] is not None:
                    raise indexer.IndexVectorError("Embedding indexes missing, duplicated or outside the batch.")
                vector = row.get("embedding")
                dimensions = indexer.validate_vector(vector, dimensions)
                ordered[idx] = vector
            vectors.extend(ordered)
            break
    return vectors


def _rerank(model, query, documents, top_n, token, deadline=None):
    if not documents:
        return []
    top_n = min(top_n, len(documents))
    timeout = min(15, _research_time_left(deadline)) if deadline is not None else 15
    response = model_client.fetch(
        "/v1/rerank", token, method="POST",
        body={"model": model, "query": query, "documents": documents, "top_n": top_n},
        timeout=timeout,
    )
    rows = response.get("results", []) if isinstance(response, dict) else []
    if not isinstance(rows, list) or not rows:
        raise ValueError("The reranker returned no valid results.")
    seen = set()
    for row in rows:
        idx = row.get("index") if isinstance(row, dict) else None
        score = row.get("relevance_score", 0) if isinstance(row, dict) else None
        if (type(idx) is not int or not 0 <= idx < len(documents) or idx in seen or
                not isinstance(score, (int, float)) or not math.isfinite(score)):
            raise ValueError("The reranker returned an invalid index or score.")
        seen.add(idx)
    return rows[:top_n]


LOG_PATH = os.path.join(RUN_DIR, "daemon.log")
LOG_MAX_BYTES = 1024 * 1024
_log_lock = threading.Lock()


def _log(message):
    """Diagnóstico em arquivo, não em stderr: o daemon é sempre iniciado detached, com
    stderr em DEVNULL (`daemon_launcher._spawn_daemon`, `pythonw` do autostart), então um
    `print` só existiria no caso em que ninguém está com problema. Rotação por tamanho
    porque nada limpa este arquivo."""
    # Sanitiza aqui, e não em cada chamador: quase toda mensagem carrega texto de origem
    # remota (corpo de resposta, mensagem de exceção), e o arquivo é lido por humano no
    # terminal e pelo próprio agente — sequência ANSI/override de direção intacta ali é o
    # mesmo vetor que `_sanitize_text` fecha nas respostas.
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {_sanitize_text(str(message))}\n"
    with _log_lock:
        try:
            os.makedirs(RUN_DIR, exist_ok=True)
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
    if sys.stderr:
        try:
            sys.stderr.write(f"[smart-tool] {message}\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


_WEB_WARNINGS = contextvars.ContextVar("web_warnings", default=None)
_WEB_TRACE = contextvars.ContextVar("web_trace", default=None)


def _web_trace(**fields):
    trace = _WEB_TRACE.get()
    if trace is not None:
        trace.update(fields)


def _web_warn(message):
    warnings = _WEB_WARNINGS.get()
    if warnings is not None and message not in warnings:
        warnings.append(message)


def _web_model_warn(message):
    if not model_client.OFFLINE.get():
        _web_warn(message)


def _gateway_problem():
    try:
        _require_valid_credential()
    except Exception as exc:
        return _project_error(exc)
    return None


def _require_valid_credential():
    current = gateway.status()
    if current["status"] != "ready":
        raise RuntimeError(f"Model gateway is not ready: {current['error']}")
    model_defaults.ensure_recommended_models(True)


def _job_checkpoint(job_id):
    with _JOBS_LOCK:
        job = _jobs.get(job_id) or {}
    if job.get("cancel_requested"):
        raise ProjectJobCancelled("Operation cancelled. Confirmed checkpoints were kept.")
    root = (job.get('arguments') or {}).get('project_root')
    if root:
        index_views.assert_current(index_views.describe(root))
    if job.get("deadline") and time.monotonic() >= job["deadline"]:
        raise TimeoutError("Job timed out. Resume it to reuse the files already confirmed.")


def _job_deadline(job_id):
    with _JOBS_LOCK:
        return (_jobs.get(job_id) or {}).get("deadline")


class ScopeNeedsAgent(RuntimeError):
    pass


class RequestTooLarge(ValueError):
    pass


def _project_error(exc):
    if isinstance(exc, ScopeNeedsAgent):
        return _sanitize_text(str(exc))[:2500]
    if isinstance(exc, urllib.error.HTTPError):
        return f"Gateway HTTP {exc.code}. " + (
            "Rate limit or quota reached; check the gateway key and resume after the backoff."
            if exc.code == 429 else "The call did not complete; check the phase and try resuming.")
    return _sanitize_text(f"{type(exc).__name__}: {exc}")[:600]


def _agent_scope_request(root, scope, problem):
    """Without a scope model the calling agent decides the scope through project_manage(action='scope')."""
    dirs = index_scope._top_level_dirs(root)
    files = index_scope._root_level_files(root, index_scope._exclude_matcher(index_scope._ALWAYS_EXCLUDE_FILES))
    listed = []
    for name in dirs:
        if sum(len(item) + 2 for item in listed) + len(name) > 800:
            break
        listed.append(name)
    shown = ", ".join(listed) + (f" (+{len(dirs) - len(listed)})" if len(listed) < len(dirs) else "")
    reason = f"model gateway unavailable ({problem})" if problem else "no scope model configured"
    stale = " The saved scope needs review (structure or version changed)." if scope else ""
    return (f"Project scope not set: {reason}.{stale} Set it yourself and repeat the search: "
            f"project_manage(action='scope', project_id='{project_identity.project_id(root)}', "
            "include=[folders with the project's code, tests and docs], exclude=[generated, dependencies, "
            f"data, builds]). Root folders: {shown or 'none'}. Loose files at the root: {len(files)} "
            "(always included unless listed in exclude).")


def _prepare_project_index(arguments, job_id, cfg, token, query_vector=None, preview=False, model=None):
    root = arguments["project_root"]
    project = project_store.get(project_identity.project_id(root)) or {}
    checkpoint = lambda: _job_checkpoint(job_id)
    checkpoint()
    _update_job(job_id, phase="scope")
    scope = index_scope.load_scope(root)
    if scope is None and not arguments.get("force_scope"):
        inherited = index_scope.sibling_scope(root)
        if inherited is not None and not index_scope.needs_rescan(root, inherited):
            index_scope.save_scope(root, inherited)
            scope = inherited
    scope_model = cfg.get("scope_model") or cfg.get("router_model")
    if arguments.get("force_scope") or (project.get('scope_dirty') and not (scope or {}).get('manual')) or scope is None or index_scope.needs_rescan(root, scope):
        problem = _gateway_problem() if scope_model else None
        if not scope_model or problem:
            raise ScopeNeedsAgent(_agent_scope_request(root, scope, problem))
        scope = index_scope.decide_scope(root, scope_model, force=True,
                                        deadline=_job_deadline(job_id), cancel_check=checkpoint)
    _update_job(job_id, phase="listing")
    included = index_scope.resolve_included_files(root, scope, cancel_check=checkpoint)
    failed_at = (scope.get("profile_error") or {}).get("at", 0)
    if ((scope.get("profile") or {}).get("version") != index_profile.PROFILE_VERSION
            and time.time() - failed_at >= index_profile.RETRY_AFTER_S and scope_model):
        _update_job(job_id, phase="profile")
        try:
            profile = index_profile.decide(root, included, scope_model, deadline=_job_deadline(job_id))
        except ProjectJobCancelled:
            raise
        except Exception as exc:
            scope = {**scope, "profile_error": {"at": time.time(), "error": _project_error(exc)}}
        else:
            scope = {key: value for key, value in scope.items() if key != "profile_error"}
            scope["profile"] = profile
        index_scope.save_scope(root, scope)
    if preview:
        _update_job(job_id, phase="preview", processed_files=0, total_files=len(included))
        eligible, skipped, characters = 0, {}, 0
        for i, rel in enumerate(included, 1):
            checkpoint()
            data, reason = indexer.read_indexable(root, rel)
            if data is None:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                eligible += 1
                characters += len(data)
            _update_job(job_id, phase="preview", processed_files=i, total_files=len(included))
        result = {"included_files": len(included), "eligible_files": eligible,
                  "bytes": characters, "skipped": skipped, "scope": scope,
                  "sample_paths": included[:30]}
        project_store.update(project_identity.project_id(root), preview=result)
        return result
    model = model or local_embedder.resolve(cfg.get("embedding_model"), indexer.info(root).get("model_id"))
    lexical = model == indexer.LEXICAL_MODEL
    if not lexical and query_vector is None:
        _update_job(job_id, phase="model_probe")
        query_vector = _embed(model, ["Smart Tool: indexing dimension check."], token,
                              deadline=_job_deadline(job_id), cancel_check=checkpoint,
                              on_retry=lambda attempt, items: _update_job(job_id, phase="indexing_retry",
                                  retry_attempt=attempt, batch_items=items))[0]
    checkpoint()
    _update_job(job_id, phase="indexing")
    current = indexer.info(root)
    full_check = bool(arguments.get("full_check") or project.get("needs_full_check") or
                      time.time() - current.get("full_scan_at", 0) >= project_monitor.FULL_CHECK_S)
    stats = indexer.reindex(
        root, included,
        None if lexical else lambda texts: _embed(model, texts, token, deadline=_job_deadline(job_id),
                             cancel_check=checkpoint, on_retry=lambda attempt, items: _update_job(
                                 job_id, phase="indexing_retry", retry_attempt=attempt, batch_items=items)),
        on_progress=lambda processed, total: _update_job(
            job_id, phase="indexing", processed_files=processed, total_files=total),
        model_id=model, expected_dimensions=None if lexical else len(query_vector),
        force_rebuild=bool(arguments.get("force_rebuild")), full_check=full_check,
        changed_paths=set(arguments.get("changed_paths") or []) | set(project.get('dirty_paths', [])), cancel_check=checkpoint,
    )
    _update_job(job_id, stats=stats, index_updated=stats.get("ready", True))
    project_store.update(project_identity.project_id(root), index=indexer.info(root),
                         last_stats=stats, last_checked=time.time(), scope_summary={
                             "include": scope.get("include", []), "exclude": scope.get("exclude", []),
                             "user_exclude": scope.get("user_exclude", []), "manual": scope.get("manual", False)})
    if not stats.get("ready", True):
        raise RuntimeError(f"{stats.get('failed', 0)} file(s) could not be read consistently. Resume indexing.")
    project_store.confirm_changes(project_identity.project_id(root), project.get('dirty_seq', 0))
    import code_graph
    code_graph.warm(root)
    if lexical:
        stats["warning"] = LEXICAL_WARNING
    return stats


def _grouped_yaml(profile, blocks, warning):
    lines = ["profile:"] + [f"  {key}: {json.dumps(_sanitize_value_deep(value), ensure_ascii=False) if isinstance(value, list) else _yaml_scalar(value)}"
                            for key, value in profile.items()]
    if warning:
        lines.append(f"warning: {_yaml_scalar(warning)}")
    for group, results in blocks.items():
        lines.append(f"{group}:" if results else f"{group}: []")
        lines += ["  " + line for line in _to_yaml(results).rstrip("\n").split("\n")] if results else []
    return "\n".join(lines) + "\n"


LOCAL_EMBEDDER_DOWN_WARNING = ("The mcp-memory local embedder is configured but not responding: index and search are lexical only "
                               "until it is back (check the mcp-memory embedding service).")
LEXICAL_WARNING = ("No embedding model: index and search are lexical only (exact words, no synonyms or cross-language matches). "
                   "Configure an embedding_model for semantic search.")


def _perform_smart_search(arguments, job_id):
    return contextvars.copy_context().run(_perform_smart_search_isolated, arguments, job_id)


def _perform_smart_search_isolated(arguments, job_id):
    _update_job(job_id, phase="auth")
    problem = _gateway_problem()
    root = arguments["project_root"]
    groups = index_profile.KINDS if arguments["group"] == "all" else (arguments["group"],)
    limits = arguments["top_k"]
    candidate_pool = 40 if arguments.get("single_language_reason") else 20
    queries = {"code": arguments["query"], "test": arguments["query"], "doc": arguments["doc_query"]}
    texts = list(dict.fromkeys(queries[group] for group in groups))
    cfg = config.load_config()
    model = local_embedder.resolve(cfg.get("embedding_model"), indexer.info(root).get("model_id"))
    warnings = []
    vectors = {}
    search_model = model
    if problem:
        model_client.OFFLINE.set(problem)
    token = "" if problem else model_client.get_token()
    if model == indexer.LEXICAL_MODEL:
        warnings.append(LOCAL_EMBEDDER_DOWN_WARNING if local_embedder.configured() and config.local_models_mode(cfg) != "off"
                        else LEXICAL_WARNING)
        try:
            _prepare_project_index(arguments, job_id, cfg, token, model=model)
        except ProjectJobCancelled:
            raise
        except Exception as exc:
            if not indexer.info(root).get("chunks"):
                raise
            warnings.append("Lexical index not updated; results from the last ready index. " + _project_error(exc))
            search_model = None
    elif problem and not local_embedder.is_local(model):
        if not indexer.info(root).get("chunks"):
            raise RuntimeError(problem)
        warnings.append("Model gateway unavailable; lexical results from the last ready index, no rerank and no index update. " + problem)
        search_model = None
    else:
        _update_job(job_id, phase="embedding_query")
        try:
            vectors = dict(zip(texts, _embed(model, texts, token, deadline=_job_deadline(job_id),
                                             cancel_check=lambda: _job_checkpoint(job_id),
                                             on_retry=lambda attempt, items: _update_job(job_id, phase="indexing_retry",
                                                 retry_attempt=attempt, batch_items=items))))
        except ProjectJobCancelled:
            raise
        except Exception as exc:
            if not indexer.info(root).get("chunks"):
                raise
            warnings.append("Embedding unavailable; lexical results from the last ready index. The update was not confirmed. " + _project_error(exc))
            search_model = None
        if vectors:
            try:
                _prepare_project_index(arguments, job_id, cfg, token, query_vector=next(iter(vectors.values())),
                                       model=model)
            except ProjectJobCancelled:
                raise
            except Exception as exc:
                if not indexer.info(root).get("chunks"):
                    raise
                warnings.append("Index update failed; results from the last ready index. " + _project_error(exc))
                search_model = None
                if indexer.info(root).get("model_id") != model:
                    vectors = {}
            if vectors and not cfg.get("rerank_model"):
                warnings.append("Rerank not configured: hybrid lexical/vector order, less precise at the top. Configure a rerank_model.")
    _job_checkpoint(job_id)
    saved_scope = index_scope.load_scope(root) or {}
    profile = index_profile.current(saved_scope.get("profile"))
    if not profile and saved_scope.get("profile_error"):
        warnings.append("Code, test and doc profile unavailable; groups come from fixed rules only, without languages. "
                        "Retrying within 1 h. " + saved_scope["profile_error"]["error"])
    if arguments.get("setup_notice"):
        warnings.append(arguments["setup_notice"])
    if arguments.get("single_language_reason"):
        warnings.append("Single-language search: " + arguments["single_language_reason"])

    try:
        import code_graph
        symbol_index, symbol_note = (code_graph.cached_symbols(root) if any(group != "doc" for group in groups)
                                     else ({}, None))
    except Exception as exc:
        symbol_index, symbol_note = None, f"Function analysis failed: {_project_error(exc)}"
    notes = []
    if symbol_index is None and any(group != "doc" for group in groups):
        (warnings if symbol_note else notes).append(
            symbol_note or "Per-chunk functions are being prepared; they will show on the next search.")
    elif symbol_note or (symbol_index or {}).get("note"):
        warnings.append(symbol_note or symbol_index["note"])
    query_terms = _symbol_terms(" ".join(queries[group] for group in groups if group != "doc"))
    _update_job(job_id, phase="searching")
    found = {group: indexer.search(root, vectors.get(queries[group]), top_k=max(limits[group] * 4, candidate_pool),
                                   query=queries[group], model_id=search_model,
                                   include=lambda path, group=group: index_profile.kind(path, profile) == group)
             for group in groups}

    def search_block(group):
        top_k = limits[group]
        query, vector, candidates = queries[group], vectors.get(queries[group]), found[group]
        ordered = [(score, "hybrid" if vector is not None else "lexical", row) for score, *row in candidates[:top_k]]
        problem = None
        if cfg.get("rerank_model") and candidates and vector is not None:
            _update_job(job_id, phase="reranking")
            try:
                reranked = _rerank(cfg["rerank_model"], query, [c[4] for c in candidates], top_k, token,
                                   deadline=_job_deadline(job_id))
                if not reranked or any(type(item.get("index")) is not int or not 0 <= item["index"] < len(candidates)
                                       for item in reranked):
                    raise ValueError("Invalid index in the reranker response.")
                ordered = [(float(item["relevance_score"]), "rerank", candidates[item["index"]][1:]) for item in reranked]
            except Exception as exc:
                problem = f"Rerank unavailable for {group}; hybrid lexical/vector order kept. " + _project_error(exc)
        rows = [{"path": path, "lines": f"{start}-{end}", "score": round(score, 6), "snippet": text[:500],
                 "ranking": ranking, "location_kind": document_text.location_kind(path),
                 **({"symbols": _chunk_symbols(symbol_index, path, start, end, text, query_terms)}
                    if symbol_index and group != "doc" else {})}
                for score, ranking, (path, start, end, text) in ordered[:top_k]]
        return rows, problem

    with concurrent.futures.ThreadPoolExecutor(len(groups)) as pool:
        futures = {group: pool.submit(contextvars.copy_context().run, search_block, group) for group in groups}
    blocks = {}
    for group, future in futures.items():
        blocks[group], problem = future.result()
        if problem:
            warnings.append(problem)
    _job_checkpoint(job_id)
    warning = " ".join(warnings)
    if warning:
        _update_job(job_id, warning=warning)
    return _grouped_yaml(index_profile.public(profile), blocks, " ".join(warnings + notes))


def _perform_project_index(arguments, job_id, preview=False):
    return contextvars.copy_context().run(_perform_project_index_isolated, arguments, job_id, preview)


def _perform_project_index_isolated(arguments, job_id, preview=False):
    _update_job(job_id, phase="auth")
    problem = _gateway_problem()
    cfg = config.load_config()
    if problem:
        model = local_embedder.resolve(cfg.get("embedding_model"), indexer.info(arguments["project_root"]).get("model_id"))
        if model != indexer.LEXICAL_MODEL and not local_embedder.is_local(model):
            raise RuntimeError(problem)
        model_client.OFFLINE.set(problem)
        return _prepare_project_index(arguments, job_id, cfg, "", preview=preview, model=model)
    return _prepare_project_index(arguments, job_id, cfg, model_client.get_token(), preview=preview)


def _run_project_job(job_id, arguments, kind):
    try:
        with index_views.pin(arguments['project_root']) as view:
            with usage_meter.job(arguments['project_root'],view,job_id):
                return _run_project_job_in_view(job_id, arguments, kind)
    except Exception as exc:
        with _JOBS_LOCK:
            pending = (_jobs.get(job_id) or {}).get('status') == 'pending'
        if pending:
            _set_job(job_id, status='error', error=_project_error(exc), failure_phase='git_view')
            project_store.update(project_identity.project_id(arguments['project_root']), status='error', last_error=_project_error(exc))
        raise


def _run_project_job_in_view(job_id, arguments, kind):
    with _JOBS_LOCK:
        job = dict(_jobs[job_id])
    project_id = job["project_id"]
    project = project_store.get(project_id) or {}
    captured_seq = project.get("dirty_seq", 0)
    _update_job(job_id, deadline=time.monotonic() + INDEX_JOB_TIMEOUT_S, started_at=time.time())
    try:
        _job_checkpoint(job_id)
        view = index_views.describe(arguments['project_root'])
        if arguments.get('_requested_view') and arguments['_requested_view'] != index_views.public(view):
            raise index_views.ViewChanged('The branch or commit changed while the job was waiting. Start again on the current view.')
        _update_job(job_id, view=index_views.public(view))
        with _project_lock(arguments["project_root"]):
            result = (_perform_smart_search(arguments, job_id) if kind == "smart_search"
                      else _perform_project_index(arguments, job_id, preview=kind == "project_preview"))
        _set_job(job_id, status="done", phase="done", result=result, error=None)
        current = project_store.get(project_id) or {}
        with _JOBS_LOCK:
            final = dict(_jobs.get(job_id) or {})
        if kind == "project_preview":
            state = "preview_ready" if not current.get("enabled") else "paused" if current.get("paused") else "dirty" if current.get("dirty_seq", 0) > current.get("indexed_seq", 0) else "ready"
            project_store.update(project_id, status=state, last_error="", last_job_id=job_id)
        else:
            updated = final.get("index_updated", False)
            dirty = current.get("dirty_seq", 0) > captured_seq
            state = ("paused" if current.get("paused") else "dirty" if dirty else
                     "degraded" if final.get("warning") else "ready")
            fields = {"status": state, "last_error": "", "last_warning": final.get("warning", ""),
                      "last_job_id": job_id, "failures": 0, "next_retry_at": 0}
            if updated:
                fields.update(indexed_seq=captured_seq, last_indexed=time.time())
            project_store.update(project_id, **fields)
        _log(f"project_job {job_id} {project_id} {kind} done elapsed={time.time()-job['created_at']:.2f}s")
        return result
    except Exception as exc:
        with _JOBS_LOCK:
            failed_phase = (_jobs.get(job_id) or {}).get("phase", "queued")
        cancelled = isinstance(exc, ProjectJobCancelled)
        error = _project_error(exc)
        _set_job(job_id, status="cancelled" if cancelled else "error", phase="cancelled" if cancelled else "error",
                 failure_phase=failed_phase, result=None, error=error)
        current = project_store.get(project_id) or {}
        failures = current.get("failures", 0) + 1
        state = "paused" if current.get("paused") else "interrupted" if cancelled else "needs_config" if failed_phase == "auth" else "error"
        project_store.update(project_id, status=state, last_error=error, last_job_id=job_id,
                             failures=failures, next_retry_at=time.time() + min(900, 30 * 2 ** min(failures, 5)))
        _log(f"project_job {job_id} {project_id} {kind} {state} phase={failed_phase} error={error}")
        raise
    finally:
        with _JOBS_LOCK:
            _JOB_FUTURES.pop(job_id, None)
            _PERSIST_LAST.pop(job_id, None)


def _run_smart_search_job(job_id, arguments):
    return _run_project_job(job_id, arguments, "smart_search")


def _submit_project_job(kind, arguments):
    with _PROJECT_ACTION_LOCK:
        return _submit_project_job_locked(kind, arguments)


def _submit_project_job_locked(kind, arguments):
    root = project_identity.display_root(arguments["project_root"])
    arguments = {**arguments, "project_root": root}
    arguments['_requested_view'] = index_views.public(index_views.describe(root, fresh=True))
    project = project_store.register(root)
    request_key = json.dumps([project["id"], kind, arguments], sort_keys=True, ensure_ascii=False)
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        existing = next((key for key, value in _jobs.items() if value.get("kind") in _PROJECT_KINDS
                         and value.get("status") == "pending" and value.get("request_key") == request_key), None)
        if existing:
            return existing, None
        if sum(value.get("kind") in _PROJECT_KINDS and value.get("status") == "pending" for value in _jobs.values()) >= SMART_SEARCH_MAX_PENDING:
            raise RuntimeError("Too many project operations in progress; wait for one to finish.")
        payload = {"kind": kind, "project_id": project["id"], "status": "pending", "phase": "queued",
                   "created_at": time.time(), "request_key": request_key, "arguments": arguments,
                   "processed_files": None, "total_files": None, "result": None, "error": None}
        _persist_project_job(job_id, payload)
        _jobs[job_id] = payload
    project_store.update(project["id"], status="queued", last_job_id=job_id,
                         enabled=kind != "project_preview" or project.get("enabled", False))
    try:
        future = SMART_SEARCH_EXECUTOR.submit(root, _run_project_job, job_id, arguments, kind)
        with _JOBS_LOCK:
            if not future.done():
                _JOB_FUTURES[job_id] = future
        def clear_future(_future):
            with _JOBS_LOCK:
                _JOB_FUTURES.pop(job_id, None)
        future.add_done_callback(clear_future)
    except Exception as exc:
        _set_job(job_id, status="error", phase="error", error=_project_error(exc))
        raise
    return job_id, future


SEARCH_LIMIT_TOTAL = 20
SEARCH_LIMIT_DEFAULT = 5


def _limit_value(raw, field_name):
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"'{field_name}' must be an integer from 1 to {SEARCH_LIMIT_TOTAL} or an object {{code, test, doc}}.")
    try:
        number = float(raw)
    except (ValueError, OverflowError):
        raise ValueError(f"'{field_name}' must be an integer, got: {raw!r}")
    if not number.is_integer():
        raise ValueError(f"'{field_name}' must be an integer, got: {raw!r}")
    return _parse_bounded_int(int(number), default=SEARCH_LIMIT_DEFAULT, min_v=1, max_v=SEARCH_LIMIT_TOTAL,
                              field_name=field_name)


def _search_limits(value, groups, saved=None):
    if value is None:
        value = saved or {}
    if not isinstance(value, dict):
        value = _limit_value(value, "top_k")
    if isinstance(value, dict) and set(value) - set(index_profile.KINDS):
        raise ValueError("top_k only accepts the keys code, test and doc.")
    limits = {}
    for group in groups:
        raw = value if not isinstance(value, dict) else value.get(group, (saved or {}).get(group, SEARCH_LIMIT_DEFAULT))
        limits[group] = _limit_value(raw, f"top_k.{group}")
    if sum(limits.values()) > SEARCH_LIMIT_TOTAL:
        raise ValueError(f"The top_k total across the requested blocks is {sum(limits.values())}; the maximum is {SEARCH_LIMIT_TOTAL}.")
    return limits


_GENERIC_SYMBOLS = {"callback", "constructor", "anonymous"}
_CONTAINER_KINDS = {"module", "template", "class", "interface"}


def _symbol_terms(text):
    return {word.casefold() for word in re.findall(r"[A-Za-zÀ-ú]{3,}", re.sub(r"([a-z])([A-Z])", r"\1 \2", text))}


def _chunk_symbols(entry, path, start, end, text, query_terms, limit=3):
    if path not in entry["parsed"]:
        return "(file not analyzed by the graph)"
    lines = text.splitlines()
    overlapping = [s for s in entry["symbols"].get(path, []) if s["start_line"] <= end and s["end_line"] >= start
                   and s.get("kind") not in _CONTAINER_KINDS]
    inner = any(s["start_line"] >= start for s in overlapping)
    picked = []
    for symbol in overlapping:
        name = symbol["name"].split("(")[0].split(".")[-1]
        if (name in _GENERIC_SYMBOLS or name.startswith("<") or "@" in name
                or (inner and symbol["start_line"] < start and symbol["end_line"] > end)):
            continue
        body = "\n".join(lines[max(symbol["start_line"] - start, 0):max(min(symbol["end_line"], end) - start + 1, 0)])
        picked.append((-len(query_terms & _symbol_terms(name + " " + body)), symbol["start_line"], name))
    return ", ".join(f"{name}:{line}" for _overlap, line, name in sorted(picked)[:limit])


def _primary_language(tags):
    return (tags[0].split("-")[0].casefold() if tags else "")


def _optional_text(arguments, name):
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f"{name} must be non-empty text of at most 4,000 characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} contains an invalid character (lone UTF-16 surrogate).")
    return value.strip()


_SMART_SEARCH_FIELDS = {"project_root", "query_identifiers", "query_comments", "doc_query",
                        "single_language_reason", "group", "top_k"}


def _reject_kind(message):
    for prefix, kind in (("Unknown fields", "unknown_field"), ("This project has identifiers", "language"),
                         ("'top_k", "limits"), ("The top_k total", "limits"), ("top_k", "limits"),
                         ("single_language_reason", "reason")):
        if message.startswith(prefix):
            return kind
    return "other"


def _handle_smart_search(arguments):
    try:
        arguments = _smart_search_arguments(arguments)
    except ValueError as exc:
        _metric("reject", kind=_reject_kind(str(exc)), message=str(exc)[:200])
        raise
    job_id, future = _submit_project_job("smart_search", arguments)
    if future is None:
        _metric("pending", reason="queued")
        return _to_json_sanitized(_smart_search_job_payload(job_id))
    try:
        result = future.result(timeout=SMART_SEARCH_SYNC_WAIT_S)
    except concurrent.futures.TimeoutError:
        if not future.done():
            _metric("pending", reason="sync_timeout")
            return _to_json_sanitized(_smart_search_job_payload(job_id))
        result = future.result()
    _mark_notice_delivered(job_id)
    return result


def _mark_notice_delivered(job_id):
    session, _client = _session_client()
    with _JOBS_LOCK:
        job = _jobs.get(job_id) or {}
    if session and job.get("status") == "done" and (job.get("arguments") or {}).get("setup_notice"):
        session["notice_delivered"] = True


def _smart_search_arguments(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("smart_search requires its arguments as a JSON object")
    project_root = arguments.get("project_root")
    if not isinstance(project_root, str) or not os.path.isdir(project_root):
        raise ValueError("smart_search requires 'project_root' pointing to an existing folder")
    group = arguments.get("group", "all")
    if group not in (*index_profile.KINDS, "all"):
        raise ValueError("group must be code, test, doc or all.")
    groups = index_profile.KINDS if group == "all" else (group,)
    unknown = set(arguments) - _SMART_SEARCH_FIELDS
    if unknown:
        raise ValueError(f"Unknown fields in smart_search: {', '.join(sorted(unknown))}. "
                         f"Accepted: {', '.join(sorted(_SMART_SEARCH_FIELDS))}.")
    identifiers = _optional_text(arguments, "query_identifiers")
    comments = _optional_text(arguments, "query_comments")
    doc_query = _optional_text(arguments, "doc_query")
    reason = _optional_text(arguments, "single_language_reason")
    if reason and len(reason) < 15:
        raise ValueError("single_language_reason must explain in at least 15 characters why a single language is enough.")
    if not identifiers and (group != "doc" or not (doc_query or comments)):
        raise ValueError("smart_search requires 'query_identifiers' (or 'doc_query' when group=doc).")
    profile = index_profile.current((index_scope.load_scope(project_root) or {}).get("profile"))
    public = index_profile.public(profile)
    identifier_language = _primary_language(public["identifier_languages"])
    other_language = next((tag for tag in public["comment_languages"] + public["identifier_languages"]
                           if _primary_language([tag]) != identifier_language), None)
    if identifier_language and other_language and group != "doc" and not comments and not reason:
        raise ValueError(
            f"This project has identifiers in {', '.join(public['identifier_languages'])} and comments in "
            f"{', '.join(public['comment_languages'])}. Fill query_identifiers with identifier terms in "
            f"{public['identifier_languages'][0]} and query_comments with the same search in {other_language}; "
            f"docs in {', '.join(public['doc_languages']) or 'undetected language'} go in doc_query. "
            "To search in a single language, explain why in single_language_reason.")
    project = project_store.get(project_identity.project_id(project_root)) or {}
    limits = _search_limits(arguments.get("top_k"), groups, project.get("search_limits"))
    query = " ".join(part for part in (identifiers, comments) if part) or doc_query
    arguments = {"project_root": project_root, "query": query, "top_k": limits, "group": group,
                 "doc_query": doc_query or comments or identifiers,
                 **({"single_language_reason": reason} if reason else {})}
    session, client = _session_client()
    if session is not None and "hook_notice" not in session:
        state = client_hooks.status(client)
        _metric("hook_status", installed=state["installed"], client_name=state["name"], error=state["error"])
        session["hook_notice"] = client_hooks.notice(state) or ""
    if session is not None and session["hook_notice"] and not session.get("notice_delivered"):
        arguments["setup_notice"] = session["hook_notice"]
    _metric("search", group=group, top_k=limits, query_comments=bool(comments), single_language_reason=reason)
    return arguments


def _handle_smart_search_result(arguments):
    job_id = arguments.get("job_id") if isinstance(arguments, dict) else None
    if not job_id:
        raise ValueError("smart_search_result requires 'job_id'")
    payload = _smart_search_job_payload(job_id)
    deadline = time.monotonic() + SMART_SEARCH_RESULT_WAIT_S
    while payload["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.25)
        payload = _smart_search_job_payload(job_id)
    _mark_notice_delivered(job_id)
    return _to_json_sanitized(payload)


def _project_payload(project):
    data = dict(project)
    data['dismissed_duplicates'] = len(data.get('dismissed_duplicates') or {})
    data['view'] = index_views.public(index_views.describe(project['root']))
    data['update_mode'] = project.get('update_mode', 'on_search')
    data["index"] = indexer.info(project["root"])
    if data['view']['git']:
        data['last_indexed'] = data['index'].get('updated_at')
        if (project.get('last_stats') or {}).get('view', {}).get('view_id') != data['view']['view_id']:
            data['last_stats'] = {}
        if project.get('enabled') and project.get('status') == 'ready' and (
                not data['index'].get('exists') or data['index'].get('git_commit') != data['view'].get('commit')):
            data['status'] = 'dirty'
    data["scope"] = index_scope.load_scope(project["root"])
    data["search_profile"] = index_profile.public(index_profile.current((data["scope"] or {}).get("profile")))
    data["search_limits"] = _search_limits(None, index_profile.KINDS, project.get("search_limits"))
    data["jobs"] = [_smart_search_job_payload(row["job_id"])
                    for row in project_store.jobs(project["id"], limit=12)]
    data["active_jobs"] = _active_project_jobs(project["id"])
    return data


def _active_project_jobs(project_id):
    with _JOBS_LOCK:
        return [key for key, value in _jobs.items() if value.get("project_id") == project_id
                and value.get("status") == "pending"]


def _cancel_project_job(job_id):
    with _JOBS_LOCK:
        job = dict(_jobs.get(job_id) or {})
        future = _JOB_FUTURES.get(job_id)
    if job.get("status") != "pending":
        return
    _update_job(job_id, cancel_requested=True)
    if future and future.cancel():
        _set_job(job_id, status="cancelled", phase="cancelled", result=None,
                 error="Operation cancelled before it started; resume it whenever you want.")
        with _JOBS_LOCK:
            _JOB_FUTURES.pop(job_id, None)


def _change_dismissal(key, project, arguments, dismiss):
    import duplicates
    finding = arguments.get("finding_id")
    if not isinstance(finding, str) or not re.fullmatch(r"[ens][0-9a-f]{16}", finding):
        raise ValueError("finding_id must be the id of a finding returned by action=duplicates.")
    with _PROJECT_ACTION_LOCK:
        current = dict(project_store.get(key).get("dismissed_duplicates") or {})
        if not dismiss:
            if current.pop(finding, None) is None:
                raise ValueError(f"{finding} is not dismissed in this project.")
            project_store.update(key, dismissed_duplicates=current)
            _metric("duplicate_restored", finding_type=duplicates.TYPE_BY_PREFIX[finding[0]])
            return {"finding_id": finding, "dismissed": False}
        kind = duplicates.LAST_FINDINGS.get(project["root"], {}).get(finding)
        if kind is None:
            raise ValueError(f"{finding} is not in this project's latest analysis; run action=duplicates and use an id from it.")
        reason = arguments.get("reason")
        if reason not in duplicates.DISMISS_REASONS:
            raise ValueError("reason must be false_positive (not a duplicate) or intentional (copy kept on purpose).")
        note = _optional_text(arguments, "note")
        if not note or not 15 <= len(note) <= duplicates.NOTE_MAX:
            raise ValueError(f"note must explain in 15 to {duplicates.NOTE_MAX} characters why this finding should not be fixed.")
        current[finding] = {"reason": reason, "note": note, "dismissed_at": time.time(), "finding_type": kind}
        project_store.update(key, dismissed_duplicates=current)
    _metric("duplicate_dismissed", finding_type=kind, reason=reason, note=note)
    return {"finding_id": finding, "dismissed": True, "reason": reason, "finding_type": kind}


def _find_duplicates(root, arguments, dismissed):
    import duplicates
    similarity = arguments.get("min_similarity", duplicates.DEFAULT_MIN_SIMILARITY)
    if isinstance(similarity, bool) or not isinstance(similarity, (int, float)) or not 0.5 <= similarity <= 1:
        raise ValueError("min_similarity must be a number between 0.5 and 1.0 (default 0.90).")
    include_tests = arguments.get("include_tests", False)
    if type(include_tests) is not bool:
        raise ValueError("include_tests must be true or false.")
    limit = _parse_bounded_int(arguments.get("limit"), default=30, min_v=1, max_v=200, field_name="limit")
    configured_model = local_embedder.resolve(config.load_config().get("embedding_model"),
                                              indexer.info(root).get("model_id"))
    if configured_model == indexer.LEXICAL_MODEL:
        configured_model = None
    view = index_views.describe(root)

    def embed(model, texts):
        with usage_meter.job(root, view, "duplicates"):
            return _embed(model, texts, model_client.get_token())

    include_dismissed = arguments.get("include_dismissed", False)
    if type(include_dismissed) is not bool:
        raise ValueError("include_dismissed must be true or false.")
    return duplicates.find(root, embed, configured_model, float(similarity), include_tests, limit, dismissed, include_dismissed)


def _with_project_id(arguments):
    """Agents know the folder, not the id: project_root resolves to the registered project's project_id."""
    root = arguments.get("project_root")
    if arguments.get("project_id") or not isinstance(root, str) or arguments.get("action") in ("register", "relocate"):
        return arguments
    wanted = project_identity.canonical_root(root)
    match = next((p for p in project_store.all_projects() if project_identity.canonical_root(p["root"]) == wanted), None)
    if not match:
        raise ValueError(f"No registered project at {root}; register it first (action=register).")
    return {**arguments, "project_id": match["id"]}


def _project_summary(project):
    data = _project_payload(project)
    index = data.get("index") or {}
    jobs = data.get("jobs") or []
    return {"project_id": data["id"], "name": data.get("name"), "root": data["root"], "status": data.get("status"),
            "enabled": data.get("enabled"), "paused": data.get("paused"), "update_mode": data.get("update_mode"),
            "files": index.get("files"), "chunks": index.get("chunks"), "last_indexed": data.get("last_indexed"),
            "view": (data.get("view") or {}).get("label"), "active_jobs": len(data.get("active_jobs") or []),
            "last_job": {k: jobs[0].get(k) for k in ("job_id", "status", "kind")} if jobs else None,
            "last_error": (data.get("last_error") or None) and str(data["last_error"])[:200]}


def _project_action(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("Pass the action in a JSON object.")
    arguments = _with_project_id(arguments)
    action = arguments.get("action", "list")
    if action == "probe":
        return _probe_index_models()
    if action in ('graph','compare','duplicates','duplicates_dismiss','duplicates_restore','docs','affected_tests'):
        # Análise somente leitura pode demorar; não prende o lock global de ações.
        key=arguments.get('project_id')
        if not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{16}',key):
            raise ValueError('Pass project_root (the folder) or a valid project_id.')
        project=project_store.get(key)
        if not project:
            raise ValueError('Project not registered.')
        if action=='duplicates':
            return _find_duplicates(project['root'], arguments, project.get('dismissed_duplicates') or {})
        if action=='docs':
            import doc_check
            include_tests=arguments.get('include_tests',False)
            if type(include_tests) is not bool:
                raise ValueError('include_tests must be true or false.')
            limit=_parse_bounded_int(arguments.get('limit'),default=30,min_v=1,max_v=200,field_name='limit')
            return doc_check.coverage(project['root'],include_tests,limit,arguments.get('view_id'))
        if action=='affected_tests':
            import affected_tests
            limit=_parse_bounded_int(arguments.get('limit'),default=30,min_v=1,max_v=200,field_name='limit')
            return affected_tests.affected(project['root'],arguments.get('base','HEAD'),limit,arguments.get('view_id'))
        if action in ('duplicates_dismiss','duplicates_restore'):
            return _change_dismissal(key, project, arguments, action == 'duplicates_dismiss')
        if action=='graph':
            import code_graph
            import code_impact
            limit=_parse_bounded_int(arguments.get('limit'),default=30,min_v=1,max_v=200,field_name='limit')
            if arguments.get('symbol') is not None:
                return code_impact.impact(project['root'],arguments['symbol'],arguments.get('view_id'),
                                          arguments.get('depth',3),limit)
            graph=code_graph.build(project['root'],arguments.get('view_id'),file_path=arguments.get('file_path'))
            return code_impact.for_agent(graph,limit) if arguments.get('file_path') else graph
        import view_compare
        return view_compare.compare(project['root'],arguments.get('left_view'),arguments.get('right_view'),
                                    arguments.get('file_path'),arguments.get('relations',True))
    # O seletor tem seu próprio ciclo de mensagens e não segura a fila de projetos.
    if action == "pick":
        import folder_picker
        root = folder_picker.choose_folder()
        if not root:
            return {"cancelled": True}
        arguments = {"action": "register", "project_root": root}
    with _PROJECT_ACTION_LOCK:
        project = project_store.get(arguments['project_id']) if isinstance(arguments.get('project_id'), str) else None
        root = project['root'] if project else arguments.get('project_root')
        with index_views.pin(root) if isinstance(root, str) and os.path.isdir(root) else contextlib.nullcontext():
            result = _project_action_locked(arguments)
    if _PROJECT_MONITOR and arguments.get("action") not in (None, "list", "status", "inspect", "usage", "storage", "pin_view", "cleanup_preview", "cleanup_commit", "search_limits", "integration", "install_hook_preview", "install_hook"):
        _PROJECT_MONITOR.refresh()
    return result


def _probe_index_models():
    global _GATEWAY_STATUS
    with _GATEWAY_LOCK:
        cfg = config.load_config()
        embedding = local_embedder.resolve(cfg.get("embedding_model"))
        embedding = None if embedding == indexer.LEXICAL_MODEL else embedding
        local_down = (config.local_models_mode(cfg) == "prefer" and local_embedder.configured()
                      and not local_embedder.info())
        models = [embedding, cfg.get("rerank_model")]
        if _GATEWAY_STATUS.get("models") == models and time.time() - _GATEWAY_STATUS.get("checked_at", 0) < 30:
            return {"gateway": dict(_GATEWAY_STATUS), "cached": True}
        result = {"checked_at": time.time(), "models": models}
        try:
            if not local_embedder.is_local(embedding) or cfg.get("rerank_model"):
                _require_valid_credential()
            token = "" if local_embedder.is_local(embedding) and not cfg.get("rerank_model") else model_client.get_token()
        except Exception as exc:
            result.update(status="needs_config", error=_project_error(exc))
        else:
            for kind, model in zip(("embedding", "rerank"), models):
                if not model:
                    result[kind] = {"status": "not_configured"}
                    continue
                if kind == "embedding" and local_down:
                    result[kind] = {"status": "error", "error": "Prefer-local mode: the mcp-memory local embedder is not "
                                    "responding; local indexes have no vector part until it is back.",
                                    "elapsed_s": 0.0}
                    continue
                start = time.monotonic()
                try:
                    if kind == "embedding":
                        vector = _embed(model, ["Smart Tool: indexing diagnostics."], token, deadline=start + 60)[0]
                        result[kind] = {"status": "ok", "dimensions": len(vector)}
                    else:
                        _rerank(model, "indexing", ["Indexing files for search."], 1, token, deadline=start + 30)
                        result[kind] = {"status": "ok"}
                except Exception as exc:
                    result[kind] = {"status": "error", "error": _project_error(exc)}
                result[kind]["elapsed_s"] = round(time.monotonic() - start, 2)
            result["status"] = "ok" if result.get("embedding", {}).get("status") == "ok" and result.get("rerank", {}).get("status") in ("ok", "not_configured") else "error"
        _GATEWAY_STATUS = result
        return {"gateway": result, "cached": False}


def _project_action_locked(arguments):
    action = arguments.get("action", "list")
    if action == "integration":
        _session, client = _session_client()
        return {"current_client": client_hooks.status(client), **integration_metrics.summary(arguments.get("days", 7))}
    if action == "install_hook_preview":
        _session, client = _session_client()
        return client_hooks.preview(client)
    if action == "install_hook":
        _session, client = _session_client()
        result = client_hooks.install(client, arguments.get("plan_id"), arguments.get("confirm"))
        _metric("install_hook", target=result["client"], changed=result["changed"])
        return result
    if action == "list":
        if not arguments.get("full"):
            return {"projects": [_project_summary(p) for p in project_store.all_projects()],
                    "detail": "Use action=status with project_id or project_root for scope, jobs and index details."}
        return {"projects": [_project_payload(p) for p in project_store.all_projects()],
                "storage_dir": indexer.INDEX_DIR,
                "gateway": dict(_GATEWAY_STATUS),
                "monitor": {"debounce_s": project_monitor.DEBOUNCE_S,
                            "reconcile_s": project_monitor.RECONCILE_S}}
    if action == "register":
        root = arguments.get("project_root")
        if not isinstance(root, str) or not os.path.isabs(root):
            raise ValueError("Pass the folder's absolute path.")
        project = project_store.register(root)
        return {"project": _project_payload(project)}
    key = arguments.get("project_id")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{16}", key):
        raise ValueError("Pass project_root (the folder) or a valid project_id.")
    project = project_store.get(key)
    if not project:
        raise ValueError("Project not registered.")
    if action == "status":
        return {"project": _project_payload(project)}
    if action == "search_limits":
        if not isinstance(arguments.get("top_k"), dict):
            raise ValueError("Pass top_k as an object {code, test, doc}; each value 1-20, total at most 20.")
        limits = _search_limits(arguments["top_k"], index_profile.KINDS, project.get("search_limits"))
        project_store.update(key, search_limits=limits)
        return {"project_id": key, "search_limits": limits}
    if action == 'inspect':
        return index_inventory.inspect(project['root'], arguments.get('view_id'), arguments.get('file_path'))
    if action=='usage':
        return usage_meter.summary(key,arguments.get('days',30),arguments.get('view_id'))
    if action=='storage':
        import project_storage
        return project_storage.inventory(project)
    active = _active_project_jobs(key)
    if action in ('pin_view','cleanup_preview','cleanup_commit'):
        if active:raise ValueError('Wait for this project\'s jobs to finish before changing storage.')
        import project_storage
        if action=='pin_view':return project_storage.pin(project,arguments.get('storage_id'),arguments.get('pinned'))
        if action=='cleanup_preview':return project_storage.preview(project,arguments.get('storage_ids'),arguments.get('clear_cache',True))
        if arguments.get('confirm') is not True:raise ValueError('Confirm the cleanup preview before applying it.')
        if not isinstance(arguments.get('plan_id'),str):raise ValueError('Pass the preview identifier.')
        return project_storage.commit(project,arguments['plan_id'])
    if action == 'policy':
        mode = arguments.get('update_mode')
        if mode not in ('on_search', 'eager'):
            raise ValueError('Choose on_search or eager for updates.')
        project_store.update(key, update_mode=mode)
        if mode == 'on_search':
            for job_id in active:
                with _JOBS_LOCK:
                    origin = ((_jobs.get(job_id) or {}).get('arguments') or {}).get('_origin')
                if origin == 'monitor':
                    _cancel_project_job(job_id)
    elif action in ("pause", "cancel"):
        project_store.update(key, paused=True, status="paused")
        requested = arguments.get("job_id")
        if requested and requested not in active:
            raise ValueError("Job is not active in this project.")
        for job_id in ([requested] if requested else active):
            _cancel_project_job(job_id)
    elif action == "watch":
        value = arguments.get("watch")
        if type(value) is not bool:
            raise ValueError("watch must be true or false.")
        project_store.update(key, watch=value, paused=False, next_retry_at=0)
        if value and project.get("enabled"):
            project_store.mark_dirty(key, "Watching enabled; checking the folder.")
    elif action in ("preview", "index", "rebuild", "resume"):
        if active:
            return {"job": _smart_search_job_payload(active[0])}
        options = {"project_root": project["root"]}
        kind = "project_preview" if action == "preview" else "project_index"
        if action == "resume":
            previous = project_store.job(arguments.get("job_id") or project.get("last_job_id"))
            if previous and previous.get("project_id") == key and previous.get("status") in ("interrupted", "error", "cancelled"):
                kind, options = previous["kind"], dict(previous["arguments"])
        if action == "rebuild":
            options.update(force_rebuild=True, full_check=True)
        if arguments.get("force_scope"):
            options["force_scope"] = True
        project_store.update(key, paused=False, next_retry_at=0)
        job_id, _future = _submit_project_job(kind, options)
        return {"job": _smart_search_job_payload(job_id)}
    elif action == "scope":
        if active:
            raise ValueError("Wait for or cancel the job before changing the scope.")
        manual = arguments.get("manual", True)
        if type(manual) is not bool:
            raise ValueError("manual must be true or false.")
        scope = dict(index_scope.load_scope(project["root"]) or {"include": ["."], "exclude": []})
        for field in ("include", "exclude", "user_exclude"):
            values = arguments.get(field, scope.get(field, []))
            if not isinstance(values, list) or len(values) > 200 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in values):
                raise ValueError(f"{field} must contain at most 200 relative paths or patterns.")
            for value in values:
                if os.path.isabs(value) or ".." in value.replace("\\", "/").split("/"):
                    raise ValueError("The scope must stay inside the project folder.")
                if field == "include" and not project_identity.within_root(project["root"], os.path.join(project["root"], value)):
                    raise ValueError("Include points outside the folder.")
            scope[field] = values
        scope.update(manual=manual, scope_version=index_scope.SCOPE_VERSION if manual else 0)
        scope.pop("profile", None)
        scope.pop("profile_error", None)
        index_scope.save_scope(project["root"], scope)
        project_store.mark_dirty(key, "Scope changed; update or check the preview.")
        project_store.update(key, preview=None)
    elif action == "remove":
        if active:
            raise ValueError("Cancel the jobs and wait for them to stop before removing the index.")
        project_store.update(key, watch=False, paused=True, enabled=False)
        removed = indexer.remove_index(project["root"])
        import glob
        for scope_id in set([key, *project_identity.legacy_ids(project["root"])]):
            for path in glob.glob(os.path.join(index_scope.SCOPE_DIR, scope_id + '*.scope.json')):
                if re.fullmatch(re.escape(scope_id) + r'(?:\.v-[a-f0-9]{16})?\.scope\.json', os.path.basename(path)):
                    os.remove(project_identity.state_path(path, index_scope.SCOPE_DIR))
        project_store.forget(key)
        return {"removed_index_files": len(removed), "source_files_preserved": True}
    elif action == "relocate":
        if active:
            raise ValueError("Wait for the jobs to stop before relocating the folder.")
        root = arguments.get("project_root")
        if not isinstance(root, str) or not os.path.isabs(root) or not os.path.isdir(root):
            raise ValueError("Pass the new absolute path of an existing folder.")
        root = project_identity.display_root(root)
        new_key = project_identity.project_id(root)
        if new_key == key:
            return {"project": _project_payload(project)}
        if project_store.get(new_key) or indexer.existing_db_path(root):
            raise ValueError("The target is already registered or indexed. Remove the target registration before relocating.")
        indexer.copy_project_indexes(project['root'], root)
        import glob
        import atomic_io
        for old_key in dict.fromkeys([key, *project_identity.legacy_ids(project['root'])]):
            for source in glob.glob(os.path.join(index_scope.SCOPE_DIR, old_key + '*.scope.json')):
                suffix = os.path.basename(source)[len(old_key):]
                if re.fullmatch(r'(?:\.v-[a-f0-9]{16})?\.scope\.json', suffix):
                    target = project_identity.state_path(os.path.join(index_scope.SCOPE_DIR, new_key + suffix), index_scope.SCOPE_DIR)
                    if not os.path.exists(target):
                        with open(source, encoding='utf-8') as stream:
                            value = json.load(stream)
                        value['root'] = project_identity.canonical_root(root)
                        atomic_io.write_secret_text(target, json.dumps(value, ensure_ascii=False))
        moved = project_store.relocate(key, root)
        with _JOBS_LOCK:
            for old_job in [j for j, data in _jobs.items() if data.get("project_id") == key]:
                _jobs.pop(old_job, None)
        return {"project": _project_payload(moved), "previous_index_preserved": True}
    else:
        raise ValueError("Unknown project management action.")
    return {"project": _project_payload(project_store.get(key))}


def _for_agent(result):
    """register/status for an agent: the project without the full result of every past job, the folder structure of
    the scope and the profile groups (one project returned 128 thousand characters, more than an MCP client shows);
    scope_summary keeps include/exclude. The projects screen keeps the full payload."""
    project = result.get("project") if isinstance(result, dict) else None
    if isinstance(project, dict) and isinstance(project.get("jobs"), list):
        project["jobs"] = [{key: job.get(key) for key in ("job_id", "status", "kind", "phase", "updated_at", "warning",
                                                          "error") if job.get(key) not in (None, "")}
                           for job in project["jobs"][:5]]
    if isinstance(project, dict):
        if isinstance(project.get("preview"), dict):
            project["preview"] = {key: project["preview"].get(key)
                                  for key in ("included_files", "eligible_files", "bytes", "skipped")}
        project.pop("scope", None)
    return result


def _handle_project_manage(arguments):
    # MCP não abre janelas; o seletor nativo pertence apenas à interface local.
    if isinstance(arguments, dict) and arguments.get("action") == "pick":
        raise ValueError("Use register with project_root or pick the folder from the tray.")
    result = _project_action(arguments)
    if isinstance(arguments, dict) and arguments.get("action") in ("register", "status"):
        result = _for_agent(result)
    return _to_json_sanitized(result)


def _enqueue_monitored_project(project_id, **options):
    with _PROJECT_ACTION_LOCK:
        project = project_store.get(project_id)
        if not project or project.get("paused") or not project.get("enabled") or not project.get("watch") or project.get('update_mode','on_search') != 'eager':
            return
        return _submit_project_job("project_index", {"project_root": project["root"], '_origin': 'monitor', **options})


def _run_search_script(executable, script_path, args, timeout_s):
    # Popen direto (não subprocess.run) porque timeout precisa matar a árvore de
    # processo inteira: Tier 2/3/4 sobem um browser real (Chromium/Firefox) como
    # filho do processo do script, e um kill só do processo direto no timeout
    # deixaria o browser órfão rodando em background. CREATE_NO_WINDOW junto de
    # CREATE_NEW_PROCESS_GROUP: o daemon roda sem console (DETACHED_PROCESS em
    # daemon_launcher), então sem essa flag cada subprocesso de busca abriria sua
    # própria janela de console visível — user-facing a cada tier tentado.
    popen_kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if _IS_WINDOWS else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        [executable, script_path, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "SMART_TOOL_USER_AGENT": version.user_agent(config.load_config().get("contact"))},
        **popen_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
        raise RuntimeError(f"timed out ({timeout_s}s)")
    if proc.returncode != 0:
        if script_path == web_search_adapters.EXTERNAL_SEARCH_SCRIPT and proc.returncode == 75:
            match = re.search(r"retry_after=(\d+)", stderr)
            seconds = int(match.group(1)) if match else WEB_PROVIDER_CIRCUIT_DEFAULT_S
            raise WebProviderRateLimited(seconds)
        # Últimos N chars, não o dump inteiro: bibliotecas como crawl4ai emitem
        # logging interno verboso (init, contexto de código, stack trace) no
        # stderr — a linha de erro relevante fica no final, não no início.
        stderr_text = stderr.strip() or f"{os.path.basename(script_path)} exited with code {proc.returncode}"
        raise RuntimeError(stderr_text[-SEARCH_SCRIPT_ERROR_MAX_CHARS:])
    return json.loads(stdout)


def _web_search_tier_order(query, scope, router_model, candidate_tiers):
    tier_order = list(candidate_tiers)
    if not router_model:
        return tier_order
    try:
        health_summary = web_search_health.summary_for_tiers(WEB_SEARCH_DEFAULT_TIER_ORDER)
        first_tier, _reason = router.decide_web_search_tier(query, scope, health_summary, router_model)
    except Exception:
        return tier_order
    # O router escolhe entre TODOS os tiers (contrato de router.py não muda por chamador),
    # mas aqui só o subconjunto pedido é válido — uma escolha fora de `candidate_tiers`
    # (ex.: um tier do grupo paralelo, quando isto é chamado só pro fallback sequencial)
    # é ignorada e a ordem default do subconjunto prevalece, nunca um erro.
    if first_tier in tier_order:
        tier_order.remove(first_tier)
        tier_order.insert(0, first_tier)
    return tier_order


def _domain_of(url):
    try:
        netloc = urllib.parse.urlparse(url).netloc
    except ValueError:
        return ""
    return netloc.lower().split(":")[0]


def _site_domains_from_query(query):
    return tuple(dict.fromkeys(match.lower().rstrip(".") for match in _SITE_OPERATOR_RE.findall(query or "")))


def _url_in_site_domains(url, domains):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _filter_site_results(rows, domains):
    if not domains:
        return rows
    return [row for row in rows if _url_in_site_domains(row.get("url", ""), domains)]


def _with_site_operator(query, domains):
    if len(domains) != 1:
        return query
    base = " ".join(_SITE_OPERATOR_RE.sub("", query).split())
    return f"{base} site:{domains[0]}"


def _site_memory_correlate(query, records=None, deadline=None, include_content=True):
    # Fail-open: qualquer exceção devolve (set(), []).
    try:
        if records is None:
            records = site_memory.snapshot()
        if not records:
            return set(), []
        cfg = config.load_config()
        embedding_model = cfg.get("embedding_model")
        rerank_model = cfg.get("rerank_model")
        if not embedding_model or not rerank_model:
            _web_model_warn("Site memory off (sources that helped before are not prioritized): embedding_model or rerank_model missing.")
            return set(), []
        threshold = config.clamp_site_memory_similarity_threshold(cfg.get("site_memory_similarity_threshold"))
        topics = list({r["topic"] for r in records})[:SITE_MEMORY_MAX_TOPICS]
        token = model_client.get_token()
        timed = {"deadline": deadline} if deadline is not None else {}
        vectors = _embed(embedding_model, [query] + topics, token, **timed)
        query_vec, topic_vecs = vectors[0], vectors[1:]
        scored_topics = [
            (topic, indexer.cosine_similarity(query_vec, vec)) for topic, vec in zip(topics, topic_vecs)
        ]
        candidates = [topic for topic, score in scored_topics if score >= threshold]
        if not candidates:
            research_metrics.log_site_memory(query, threshold, scored_topics, recalled=0)
            return set(), []
        reranked = _rerank(rerank_model, query, candidates, len(candidates), token, **timed)
        rerank_scores = {}
        for item in reranked:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                rerank_scores[candidates[idx]] = float(item.get("relevance_score") or 0)
        similar_topics = {
            topic for topic, score in rerank_scores.items()
            if score >= SITE_MEMORY_RERANK_MIN_SCORE
        }
        if not similar_topics:
            research_metrics.log_site_memory(
                query, threshold, scored_topics, recalled=0,
                rerank_scores=rerank_scores, rerank_threshold=SITE_MEMORY_RERANK_MIN_SCORE,
            )
            return set(), []
        matching = [r for r in records if r["topic"] in similar_topics]
        boost_domains = {r["domain"] for r in matching}
        if not include_content:
            research_metrics.log_site_memory(
                query, threshold, scored_topics, recalled=0,
                rerank_scores=rerank_scores, rerank_threshold=SITE_MEMORY_RERANK_MIN_SCORE,
            )
            return boost_domains, []
        seen_urls = set()
        recalled = []
        for r in matching:
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            recalled.append({
                "title": r["title"], "url": r["url"], "snippet": r["snippet"],
                "tier": "memoria", "recalled_from_topic": r["topic"],
            })
        research_metrics.log_site_memory(
            query, threshold, scored_topics, recalled=len(recalled),
            rerank_scores=rerank_scores, rerank_threshold=SITE_MEMORY_RERANK_MIN_SCORE,
        )
        return boost_domains, recalled
    except Exception as exc:
        _web_model_warn(f"Site memory failed on this call: {type(exc).__name__}: {exc}"[:300])
        return set(), []


def _remember_site_results(query, tier_name, raw_results):
    # Fail-open, mesmo contrato de _record_tier_health.
    try:
        for item in raw_results:
            domain = _domain_of(item.get("url", ""))
            if not domain:
                continue
            site_memory.record(
                query, domain, tier_name,
                title=item.get("title", ""), url=item.get("url", ""), snippet=item.get("snippet", ""),
            )
    except Exception:
        pass


def _provider_config_id(tier_name):
    keys = config.load_config().get("api_keys") or {}
    blob = keys.get(tier_name, "") if isinstance(keys, dict) else ""
    return hashlib.sha256(str(blob).encode("utf-8")).digest()


def _provider_circuit_wait(tier_name, config_id):
    with _web_provider_circuit_lock:
        state = _web_provider_circuits.get(tier_name)
        if not state or state[1] != config_id:
            _web_provider_circuits.pop(tier_name, None)
            return 0
        left = state[0] - time.monotonic()
        if left <= 0:
            _web_provider_circuits.pop(tier_name, None)
            return 0
        return left


def _open_provider_circuit(tier_name, config_id, seconds):
    with _web_provider_circuit_lock:
        _web_provider_circuits[tier_name] = (
            time.monotonic() + min(WEB_PROVIDER_CIRCUIT_MAX_S, max(WEB_PROVIDER_CIRCUIT_MIN_S, seconds)),
            config_id,
        )


class TierSkipped(RuntimeError):
    """Self-imposed pause (provider circuit open): not a failure of the provider."""


BROWSER_SERVICE = browser_service_client.BrowserService(web_search_adapters.CAMOUFOX_VENV_PYTHON,
                                                        web_search_adapters.BROWSER_SERVICE_SCRIPT)
BROWSER_CAPTCHA_PAUSE_S = 3600
_BROWSER_CIRCUIT_ID = b"browser-service"


def _run_browser_tier(tier_name, adapter, query, num, timeout):
    wait = _provider_circuit_wait(tier_name, _BROWSER_CIRCUIT_ID)
    if wait:
        raise browser_service_client.BrowserSkipped(f"{tier_name}: paused after a captcha for {int(wait) + 1}s more")
    try:
        raw_results = BROWSER_SERVICE.search(adapter["service_engine"], query, num, timeout)
    except browser_service_client.BrowserCaptcha:
        _open_provider_circuit(tier_name, _BROWSER_CIRCUIT_ID, BROWSER_CAPTCHA_PAUSE_S)
        raise
    if not raw_results:
        raise RuntimeError("zero results")
    return raw_results[:num]


def _run_tier(tier_name, query, num, timeout_s=None):
    adapter = web_search_adapters.SPECS[tier_name]
    if "service_engine" in adapter:
        timeout = adapter["timeout_s"] if timeout_s is None else min(adapter["timeout_s"], timeout_s)
        return _run_browser_tier(tier_name, adapter, query, num, timeout)
    config_id = None
    if tier_name in config.WEB_PROVIDER_NAMES:
        config_id = _provider_config_id(tier_name)
        wait = _provider_circuit_wait(tier_name, config_id)
        if wait:
            raise TierSkipped(f"{tier_name}: quota backoff for {int(wait) + 1}s more")
    args = [query, "--num", str(num), *adapter["extra_args"]]
    timeout = adapter["timeout_s"] if timeout_s is None else min(adapter["timeout_s"], timeout_s)
    try:
        raw_results = _run_search_script(adapter["executable"], adapter["script"], args, timeout)
    except WebProviderRateLimited as exc:
        if config_id is not None:
            _open_provider_circuit(tier_name, config_id, exc.retry_after)
        raise
    if not raw_results:
        raise RuntimeError("zero results")
    return raw_results[:num]


def _merge_diverse_results(collected, num, boost_domains=None):
    # Round-robin entre as fontes que responderam, não concatenado por ordem de
    # chegada -- senão a fonte mais rápida preenche os `num` resultados sozinha e as
    # outras nunca aparecem na resposta, o oposto do propósito do fan-out. `boost_domains`
    # (memória por site, best-effort) só reordena dentro de cada tier -- a fonte diversa
    # do round-robin em si nunca é comprometida por causa da memória.
    if boost_domains:
        collected = [
            (tier, sorted(raw, key=lambda item: _domain_of(item.get("url", "")) not in boost_domains))
            for tier, raw in collected
        ]
    rank = {tier: i for i, tier in enumerate(web_search_adapters.MERGE_PRIORITY)}
    collected = sorted(collected, key=lambda pair: rank.get(pair[0], len(rank)))
    iterators = [iter(raw) for _, raw in collected]
    tier_names = [tier for tier, _ in collected]
    seen_urls = set()
    merged = []
    active = list(range(len(iterators)))
    while active and len(merged) < num:
        for idx in list(active):
            try:
                item = next(iterators[idx])
            except StopIteration:
                active.remove(idx)
                continue
            url = item.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            entry = {
                "title": item.get("title", ""), "url": url,
                "snippet": item.get("snippet", ""), "tier": tier_names[idx],
            }
            if "recalled_from_topic" in item:
                entry["recalled_from_topic"] = item["recalled_from_topic"]
            merged.append(entry)
            if len(merged) >= num:
                break
    return merged


def _fan_out_web_search(query, num, budget_s=None, site_domains=()):
    # Dispara todo o grupo `fan_out=True` de uma vez em WEB_SEARCH_EXECUTOR (pool
    # compartilhado, nunca fechado) em vez de num ThreadPoolExecutor local: um `with`
    # local bloquearia no `__exit__` até o tier mais lento terminar, mesmo depois do
    # orçamento já ter sido atingido — aqui, o que não responde a tempo continua
    # rodando sozinho em background (só telemetria, via done_callback) e a resposta
    # sai sem esperar por ele.
    futures = {}
    for tier_name in web_search_adapters.FAN_OUT_TIERS:
        fut = WEB_SEARCH_EXECUTOR.submit(_run_tier, tier_name, query, num)
        futures[fut] = tier_name

    def _record_on_done(tier_name):
        def _cb(fut):
            try:
                fut.result()
                _record_tier_health(tier_name, success=True)
            except Exception as exc:
                _record_tier_failure(tier_name, exc)
        return _cb

    for fut, tier_name in futures.items():
        fut.add_done_callback(_record_on_done(tier_name))

    wait_s = WEB_SEARCH_FAN_OUT_BUDGET_S if budget_s is None else min(WEB_SEARCH_FAN_OUT_BUDGET_S, budget_s)
    done, _pending = concurrent.futures.wait(futures.keys(), timeout=wait_s)
    collected = []
    errors = []
    for fut in done:
        tier_name = futures[fut]
        try:
            raw_results = fut.result()
            raw_results = _filter_site_results(raw_results, site_domains)
            if not raw_results:
                continue
            collected.append((tier_name, raw_results))
            _remember_site_results(query, tier_name, raw_results)
        except Exception as exc:
            errors.append(_sanitize_text(f"{tier_name}: {type(exc).__name__}: {exc}"))
    return collected, errors


WEB_SEARCH_FAILED_KEY = "tool:web_search"


def _handle_web_search(arguments):
    return contextvars.copy_context().run(_handle_web_search_isolated, arguments)


def _handle_web_search_isolated(arguments):
    try:
        return _handle_web_search_body(arguments)
    except Exception as exc:
        web_fetch.mark_failed(WEB_SEARCH_FAILED_KEY)
        trace = _WEB_TRACE.get()
        if trace is not None and "started" in trace:
            started = trace.pop("started")
            _metric("web_search", error=type(exc).__name__, elapsed_s=round(time.monotonic() - started, 2), **trace)
        raise


def _handle_web_search_body(arguments):
    query = arguments.get("query")
    if not query:
        raise ValueError("web_search requires 'query'")
    if len(query) > WEB_SEARCH_MAX_QUERY_CHARS:
        raise ValueError(f"'query' too long ({len(query)} characters) — maximum {WEB_SEARCH_MAX_QUERY_CHARS}")
    scope = arguments.get("scope")
    depth = arguments.get("depth")
    num = _parse_bounded_int(arguments.get("num"), default=8, min_v=1, max_v=20, field_name="num")
    query_en = arguments.get("query_en")
    if depth in (None, "quick"):
        if not isinstance(query_en, str) or not query_en.strip():
            raise ValueError("web_search with depth='quick' requires 'query_en': the query in English "
                             "(repeat query if it is already in English)")
        if len(query_en) > WEB_SEARCH_MAX_QUERY_CHARS:
            raise ValueError(f"'query_en' too long ({len(query_en)} characters) — maximum {WEB_SEARCH_MAX_QUERY_CHARS}")

    if depth in ("deep", "research"):
        _require_valid_credential()
    if depth == "deep":
        # Modo determinístico por código, não decidido por modelo: profundidade
        # "deep" é sempre assíncrona e sempre Tier 4 — ver ADR 0006.
        job_id = uuid.uuid4().hex
        _set_job(job_id, status="pending", result=None, error=None, created_at=time.time())
        try:
            DEEP_JOB_EXECUTOR.submit(_run_deep_job, job_id, query, scope)
        except Exception as exc:
            # Se o submit() em si falhar (pool fechado/saturado), o job já foi
            # registrado como "pending" — sem isto, ficaria assim pra sempre,
            # já que nenhum worker nunca chegaria a chamar _set_job de novo.
            _set_job(job_id, status="error", result=None, error=_sanitize_text(f"{type(exc).__name__}: {exc}"), created_at=time.time())
        return _to_json_sanitized({"job_id": job_id, "status": "pending"})

    if depth == "research":
        job_id = uuid.uuid4().hex
        _set_job(job_id, status="pending", result=None, error=None, created_at=time.time())
        try:
            RESEARCH_JOB_EXECUTOR.submit(_run_research_job, job_id, query, num, scope)
        except Exception as exc:
            _set_job(job_id, status="error", result=None, error=_sanitize_text(f"{type(exc).__name__}: {exc}"), created_at=time.time())
        return _to_json_sanitized({"job_id": job_id, "status": "pending"})

    warnings = []
    _WEB_WARNINGS.set(warnings)
    _WEB_TRACE.set({"started": time.monotonic(), "cache": "fresh", "probe": False,
                    "bilingual": _normalized(query_en) != _normalized(query)})
    problem = _gateway_problem()
    _web_trace(degraded=bool(problem))
    if problem:
        model_client.OFFLINE.set(problem)
        warnings.append("Model gateway unavailable: web sources only, no coverage assessment, "
                        "no similarity reuse and no volatility judgment. " + problem)
    cfg = config.load_config()
    timeout_s = config.clamp_quick_search_timeout(cfg.get("quick_search_timeout_s"))
    deadline = time.monotonic() + timeout_s
    cache_query = query if _normalized(query_en) == _normalized(query) else f"{query} | {query_en}"
    try:
        cached, topic_vector = _validated_cache_result(
            "quick", cache_query, scope, cfg, "" if problem else model_client.get_token(), deadline, requested_num=num,
        )
    except Exception as exc:
        _log(f"research cache unavailable on this call: {type(exc).__name__}: {exc}")
        _web_warn(f"Research cache unavailable on this call: {type(exc).__name__}: {exc}"[:300])
        cached, topic_vector = None, None
    if cached:
        entry, status = cached
        _web_trace(cache=status, volatility=(entry.get("classification") or {}).get("volatility"))
        return _quick_output(_annotate_quick_results(
            entry["result"]["rows"][:num], entry["classification"], _cache_trace(entry, status),
        ), warnings)
    direct_security = freshness.npm_version_security_only(query)
    if freshness.npm_version_only(query) or direct_security:
        evidence = freshness.npm_registry_evidence(freshness.npm_package(query))
        if evidence:
            results = [evidence]
            if direct_security:
                osv = freshness.npm_osv_evidence(evidence["npm_package"], evidence["npm_version"])
                if osv:
                    results.append(osv)
            if not direct_security or (len(results) == 2 and num >= 2):
                results = results[:num]
                classification = _store_fresh_research(
                    "quick", cache_query, scope, {"rows": results, "requested_num": num},
                    results, cfg, topic_vector,
                )
                return _quick_output(_annotate_quick_results(results, classification), warnings)
    results = _bilingual_search(query, query_en, num, scope, deadline)
    results = _with_npm_evidence(query, results, num)
    classification = _store_fresh_research(
        "quick", cache_query, scope, {"rows": results, "requested_num": num}, results, cfg,
        topic_vector,
    )
    return _quick_output(_annotate_quick_results(results, classification), warnings)


def _quick_output(rows, warnings):
    if not rows:
        web_fetch.mark_failed(WEB_SEARCH_FAILED_KEY)
    trace = _WEB_TRACE.get()
    if trace is not None:
        started = trace.pop("started")
        _metric("web_search", warnings=len(warnings), elapsed_s=round(time.monotonic() - started, 2), **trace)
    lines = ["results:" if rows else "results: []"]
    lines += ["  " + line for line in _to_yaml(rows).rstrip("\n").split("\n")] if rows else []
    if warnings:
        lines += ["warnings:"] + [f"  - {_yaml_scalar(warning)}" for warning in warnings]
    return "\n".join(lines) + "\n"


def _research_time_left(deadline):
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("total research time exhausted")
    return left


def _web_results_need_more(query, results, num, scope, model, assess_semantic, deadline=None):
    """Decide se outra fonte acrescentaria cobertura útil à busca rápida."""
    if not results:
        return True
    domains = {_domain_of(row.get("url", "")) for row in results}
    domains.discard("")
    if sum(bool((row.get("snippet") or "").strip()) for row in results) < max(1, len(results) // 2):
        return True
    if assess_semantic and model:
        try:
            left = _research_time_left(deadline)
            if left is not None and left < 0.5:
                raise TimeoutError("search assessment deadline exhausted")
            need_more, _reason = router.assess_web_search_results(
                query, results, num, scope, model, timeout=min(12, left) if left else 12,
            )
            return need_more
        except Exception as exc:
            _web_model_warn(f"Model coverage assessment unavailable; mechanical criterion used: {type(exc).__name__}: {exc}"[:300])
    # `num` é teto, não uma meta obrigatória. Em research o pesquisador e o
    # crítico julgam cobertura e diversidade depois da busca; exigir dois
    # domínios aqui nunca terminaria uma consulta restrita por `site:`.
    minimum = min(num, 3 if assess_semantic else 2)
    return len(results) < minimum or (assess_semantic and len(domains) < min(num, 2))


def _memory_snapshot():
    try:
        return site_memory.snapshot()
    except Exception:
        return []


def _search_web_once(query, num, scope, deadline=None, assess_semantic=False,
                     site_domains=None, allow_recall=True, memory_before=None, single_browser=False):
    """Busca uma rodada; amplia fontes só quando o resultado segue insuficiente."""
    # O fan-out grava achados em site_memory. Congelar o estado antes dele impede
    # comparar a query com os próprios resultados recém-gravados (score 1,0).
    if memory_before is None:
        memory_before = _memory_snapshot()
    domains = _site_domains_from_query(query) if site_domains is None else tuple(site_domains)
    fanout_options = {"budget_s": _research_time_left(deadline)}
    if domains:
        fanout_options["site_domains"] = domains
    collected, errors = _fan_out_web_search(query, num, **fanout_options)
    if domains:
        collected = [(tier, filtered) for tier, raw in collected
                     if (filtered := _filter_site_results(raw, domains))]
    try:
        _research_time_left(deadline)
    except TimeoutError:
        if collected:
            return _merge_diverse_results(collected, num)
        raise
    correlation_options = {"deadline": deadline} if deadline is not None else {}
    if not allow_recall:
        correlation_options["include_content"] = False
    boost_domains, recalled = _site_memory_correlate(query, memory_before, **correlation_options)
    recalled = _filter_site_results(recalled, domains)
    if recalled and allow_recall:
        # `recalled` entra como tier "memoria" no round-robin/dedup de _merge_diverse_results.
        collected.append(("memoria", recalled))
    results = _merge_diverse_results(collected, num, boost_domains) if collected else []
    cfg = config.load_config()
    router_model = cfg.get("router_model")
    if not _web_results_need_more(query, results, num, scope, router_model, assess_semantic, deadline):
        return results

    # Chave opcional: os adaptadores escolhem keyless quando nenhuma foi configurada.
    # Provedores com cota por IP não participam do fan-out em toda chamada.
    for tier_name in web_search_adapters.HTTP_FALLBACK_TIERS:
        try:
            left = _research_time_left(deadline)
        except TimeoutError:
            if results:
                return results
            raise
        try:
            raw_results = _filter_site_results(
                _run_tier(tier_name, query, num, timeout_s=left), domains,
            )
            if not raw_results:
                raise RuntimeError("zero results in the requested domain")
        except Exception as exc:
            _record_tier_failure(tier_name, exc)
            errors.append(_sanitize_text(f"{tier_name}: {type(exc).__name__}: {exc}"))
            continue
        _record_tier_health(tier_name, success=True)
        _remember_site_results(query, tier_name, raw_results)
        collected.insert(0, (tier_name, raw_results))
        results = _merge_diverse_results(collected, num, boost_domains)
        if not _web_results_need_more(query, results, num, scope, router_model, assess_semantic, deadline):
            return results

    # Navegadores só entram se as fontes HTTP ainda não cobrem a consulta.
    try:
        left = _research_time_left(deadline)
    except TimeoutError:
        if results:
            return results
        raise
    # Quick mode keeps camoufox first: a startpage chosen first runs until the deadline when it fails.
    browser_model = router_model if not single_browser and (left is None or left > 45) else None
    tier_order = _web_search_tier_order(query, scope, browser_model, web_search_adapters.BROWSER_FALLBACK_TIERS)
    for tier_name in tier_order:
        try:
            left = _research_time_left(deadline)
        except TimeoutError:
            if results:
                return results
            raise
        try:
            raw_results = _filter_site_results(
                _run_tier(tier_name, query, num, timeout_s=left), domains,
            )
            if not raw_results:
                raise RuntimeError("zero results in the requested domain")
        except Exception as exc:
            _record_tier_failure(tier_name, exc)
            errors.append(_sanitize_text(f"{tier_name}: {type(exc).__name__}: {exc}"))
            continue
        _record_tier_health(tier_name, success=True)
        _remember_site_results(query, tier_name, raw_results)
        collected.insert(0, (tier_name, raw_results))
        results = _merge_diverse_results(collected, num, boost_domains)
        # Quick mode: a failing browser runs until the deadline (83-99 s measured), so the next one only covers a failure.
        if single_browser or not _web_results_need_more(query, results, num, scope, router_model, assess_semantic, deadline):
            return results

    if results:
        return results

    raise RuntimeError("All search tiers failed - " + "; ".join(errors))


def _cache_namespace(depth, scope):
    normalized = " ".join(str(scope or "").casefold().split())
    return depth + "|" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _cache_match(depth, query, scope, cfg, token, deadline):
    """Exato primeiro; aproximação semântica exige confirmação do mesmo fato."""
    entries = research_cache.candidates(_cache_namespace(depth, scope), query)
    if not entries:
        return None, None
    normalized = _normalized(query)
    for entry in entries:
        if _normalized(entry.get("query")) == normalized:
            stored = entry.get("topic_vector") or {}
            return entry, (stored.get("values") if stored.get("model") == cfg.get("embedding_model") else None)
    if freshness.npm_version_only(query) or freshness.npm_version_security_only(query):
        package = freshness.npm_package(query)
        wants_security = freshness.npm_security_intent(query)
        for entry in entries:
            classified = entry.get("classification") or {}
            if (classified.get("npm_package") == package
                    and bool(classified.get("security_intent")) == wants_security):
                return entry, None
    if model_client.OFFLINE.get():
        return None, None
    embed_model, rerank_model = cfg.get("embedding_model"), cfg.get("rerank_model")
    match_model = cfg.get("router_model") or cfg.get("research_model")
    if not embed_model or not rerank_model or not match_model:
        missing = [name for name, value in (("embedding_model", embed_model), ("rerank_model", rerank_model),
                                            ("router_model", match_model)) if not value]
        _web_model_warn("Reuse of similar searches off (only the same question is reused): missing " + ", ".join(missing) + ".")
        return None, None
    entries = [entry for entry in entries
               if (entry.get("topic_vector") or {}).get("model") == embed_model]
    if not entries:
        return None, None
    try:
        vector = _embed(embed_model, [query], token, deadline=deadline)[0]
        scored = sorted(((indexer.cosine_similarity(vector, entry["topic_vector"]["values"]), i)
                         for i, entry in enumerate(entries)), reverse=True)
        candidates = [i for score, i in scored[:10] if score >= 0.45]
        if not candidates:
            return None, vector
        topics = [str(entries[i].get("query") or "") for i in candidates]
        ranked = _rerank(rerank_model, query, topics, min(3, len(candidates)), token, deadline=deadline)
        for row in ranked[:3]:
            index = row.get("index")
            if not isinstance(index, int) or not 0 <= index < len(candidates):
                continue
            if float(row.get("relevance_score") or 0) < 0.12:
                continue
            entry = entries[candidates[index]]
            if freshness.same_fact(query, entry["query"],
                                   entry.get("classification", {}).get("subject", ""), match_model,
                                   timeout=min(12, _research_time_left(deadline))):
                return entry, vector
    except Exception as exc:
        _log(f"semantic cache match failed: {type(exc).__name__}: {exc}")
        _web_model_warn(f"Reuse of similar searches failed on this call: {type(exc).__name__}: {exc}"[:300])
        return None, None
    return None, vector


def _bilingual_search(query, query_en, num, scope, deadline):
    if _normalized(query_en) == _normalized(query):
        return _search_web_once(query, num, scope, deadline=deadline, assess_semantic=True, allow_recall=False,
                                single_browser=True)
    domains = tuple(dict.fromkeys(_site_domains_from_query(query) + _site_domains_from_query(query_en))) or None
    options = {"deadline": deadline, "assess_semantic": True, "allow_recall": False, "single_browser": True,
               "site_domains": domains, "memory_before": _memory_snapshot()}
    if domains:
        query, query_en = _with_site_operator(query, domains), _with_site_operator(query_en, domains)
    context = contextvars.copy_context()
    english = WEB_LANG_EXECUTOR.submit(context.run, _search_web_once, query_en, num, scope, **options)
    original_error = english_error = None
    try:
        original = _search_web_once(query, num, scope, **options)
    except Exception as exc:
        original, original_error = [], exc
    try:
        translated = english.result()
    except Exception as exc:
        translated, english_error = [], exc
    if original_error and english_error:
        raise RuntimeError(f"search failed in both languages — original: {original_error}; English: {english_error}")
    if original_error or english_error:
        failed, error = ("original", original_error) if original_error else ("English", english_error)
        _log(f"{failed} search failed; results from the other language only: {type(error).__name__}: {error}")
        _web_warn(f"{failed} search failed; results from the other language only: {type(error).__name__}: {error}"[:300])
    merged, seen = [], set()
    for pair in itertools.zip_longest(translated, original):
        for row in pair:
            if row and (not row.get("url") or row["url"] not in seen):
                seen.add(row.get("url"))
                merged.append(row)
    return merged[:num]


def _validated_cache_result(depth, query, scope, cfg, token, deadline, requested_num=None):
    """Devolve ((entrada, status) ou None, vetor da consulta) para reaproveitar na gravação."""
    entry, topic_vector = _cache_match(depth, query, scope, cfg, token, deadline)
    if not entry:
        return None, topic_vector
    if (depth == "quick" and requested_num is not None
            and int(entry.get("result", {}).get("requested_num") or 0) < requested_num):
        return None, topic_vector
    pending = (entry.get("classification") or {}).get("volatility") == "pending"
    if (pending and not model_client.OFFLINE.get() and time.time() - entry["saved_at"] > WEB_CACHE_PENDING_GRACE_S
            and _claim_completion(entry["id"], entry["saved_at"])):
        sources = entry["result"].get("rows") if depth == "quick" else entry["result"].get("sources")
        context = contextvars.copy_context()
        WEB_CACHE_EXECUTOR.submit(context.run, _complete_research, _cache_namespace(depth, scope), entry["query"],
                                  entry["saved_at"], sources or [], entry["classification"], cfg, None)
    hit = _revalidated(depth, entry, cfg, deadline)
    if hit and topic_vector is not None and not pending and _normalized(entry["query"]) != _normalized(query):
        research_cache.put(_cache_namespace(depth, scope), query, entry["result"], entry["classification"],
                           cfg.get("embedding_model"), topic_vector, origin=entry)
    return hit, topic_vector


def _normalized(text):
    return " ".join(str(text or "").casefold().split())


def _mark_checked(entry, status):
    research_cache.mark_validated(entry["id"])
    entry["validated_at"] = time.time()
    return entry, status


def _cache_trace(entry, status):
    stamp = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    return {"status": status, "searched_at": stamp(entry["saved_at"]),
            "checked_at": stamp(entry["validated_at"]),
            "cached_query": entry.get("origin_query") or entry["query"]}


def _revalidated(depth, entry, cfg, deadline):
    classification = entry.get("classification") or {}
    level = classification.get("volatility", "high")
    ttl = freshness.TTL_SECONDS.get(level, 0)
    if ttl and time.time() - entry["validated_at"] < ttl:
        return entry, "within_ttl"
    npm_package = classification.get("npm_package")
    npm_version = classification.get("npm_version")
    if npm_package and npm_version:
        current = freshness.npm_registry_evidence(npm_package)
        if current:
            if current["npm_version"] != npm_version:
                research_cache.discard(entry["id"])
                return None
            if not classification.get("security_intent"):
                return _mark_checked(entry, "registry_verified")
            baseline = classification.get("npm_advisories")
            if baseline is not None:
                current_ids = freshness.npm_advisories(npm_package, npm_version)
                if current_ids is not None:
                    if current_ids == baseline:
                        return _mark_checked(entry, "registry_osv_verified")
                    research_cache.discard(entry["id"])
                    return None
    if model_client.OFFLINE.get():
        return None
    probe_query = str(classification.get("validation_query") or "").strip()
    if not probe_query:
        return None
    domains = _site_domains_from_query(entry["query"])
    probe_query = _with_site_operator(probe_query, domains)[:WEB_SEARCH_MAX_QUERY_CHARS]
    try:
        probe_deadline = min(deadline, time.monotonic() + 45)
        _web_trace(probe=True)
        fresh = _search_web_once(
            probe_query, 5, None, deadline=probe_deadline, assess_semantic=False,
            site_domains=domains or None, allow_recall=False,
        )
        cached_content = entry["result"].get("rows", []) if depth == "quick" else entry["result"]
        model = cfg.get("router_model") or cfg.get("research_model")
        verdict, reason = freshness.validate(entry["query"], cached_content,
                                               classification, fresh, model,
                                               timeout=min(12, _research_time_left(deadline)))
    except Exception as exc:
        _log(f"change check failed; searching again: {type(exc).__name__}: {exc}")
        _web_model_warn(f"Check of the cached research failed; search redone: {type(exc).__name__}: {exc}"[:300])
        return None
    _web_trace(verdict=verdict)
    if verdict == "current":
        return _mark_checked(entry, "revalidated")
    _log(f"change check: {verdict}; searching again ({reason})")
    if verdict == "changed":
        research_cache.discard(entry["id"])
    return None


def _store_fresh_research(depth, query, scope, result, sources, cfg, topic_vector=None):
    """Grava já com volatilidade pendente; o julgamento e o vetor completam em segundo plano."""
    if not sources:
        return None
    classification = freshness.pending_classification(query)
    npm_source = next((row for row in sources if row.get("tier") == "npm_registry"), None)
    if npm_source:
        classification["npm_package"] = npm_source["npm_package"]
        classification["npm_version"] = npm_source["npm_version"]
        classification["security_intent"] = freshness.npm_security_intent(query)
        if classification["security_intent"]:
            osv_source = next((row for row in sources if row.get("tier") == "osv_api"), None)
            classification["npm_advisories"] = (osv_source["osv_advisory_ids"]
                                                 if osv_source else None)
    namespace = _cache_namespace(depth, scope)
    embed_model = cfg.get("embedding_model")
    saved_at = research_cache.put(namespace, query, result, classification,
                                  embed_model if topic_vector else None, topic_vector)
    if saved_at is not None and not model_client.OFFLINE.get():
        context = contextvars.copy_context()
        WEB_CACHE_EXECUTOR.submit(context.run, _complete_research, namespace, query, saved_at,
                                  sources, classification, cfg, topic_vector)
    return classification


_COMPLETING = set()
_COMPLETING_LOCK = threading.Lock()


def _claim_completion(entry_id, saved_at):
    with _COMPLETING_LOCK:
        if (entry_id, saved_at) in _COMPLETING:
            return False
        _COMPLETING.add((entry_id, saved_at))
        return True


def _complete_research(namespace, query, saved_at, sources, pending, cfg, topic_vector):
    model = cfg.get("router_model") or cfg.get("research_model")
    classification = freshness.classify(query, sources, model, timeout=WEB_CACHE_CLASSIFY_TIMEOUT_S)
    if classification.get("error"):
        _log(f"volatility not judged; reuse requires a check: {classification['error']}")
    classification.update({key: value for key, value in pending.items()
                           if key.startswith("npm_") or key == "security_intent"})
    embed_model = cfg.get("embedding_model")
    if topic_vector is None and embed_model:
        try:
            topic_vector = _embed(embed_model, [query], model_client.get_token())[0]
        except Exception as exc:
            _log(f"research vector not generated; reuse by exact text only: {type(exc).__name__}: {exc}")
    if not research_cache.complete(namespace, query, saved_at, classification,
                                   embed_model if topic_vector else None, topic_vector):
        _log("research classification not saved to the cache (entry replaced or cache unavailable)")


def _with_npm_evidence(query, results, num):
    package = freshness.npm_package(query)
    if not package:
        return results
    evidence = freshness.npm_registry_evidence(package)
    if not evidence:
        return results
    domains = _site_domains_from_query(query)
    if domains and not _url_in_site_domains(evidence["url"], domains):
        return results
    official = [evidence]
    if freshness.npm_security_intent(query):
        osv = freshness.npm_osv_evidence(package, evidence["npm_version"])
        if osv:
            official.append(osv)
    if domains:
        official = [row for row in official if _url_in_site_domains(row["url"], domains)]
    seen = {row["url"] for row in official}
    return official[:num] + [row for row in results if row.get("url") not in seen][:max(0, num - len(official))]


def _annotate_quick_results(rows, classification, cache=None):
    level = (classification or {}).get("volatility")
    labels = {item.get("url"): item.get("volatility")
              for item in (classification or {}).get("items", [])}
    result = []
    for row in rows:
        item = dict(row)
        if "osv_advisory_ids" in item:
            ids = item.pop("osv_advisory_ids")
            item["osv_advisory_count"] = len(ids) if isinstance(ids, list) else 0
        if level and level != "pending":
            item["volatility"] = labels.get(item.get("url")) or level
        if cache:
            item["source_tier"] = item.get("tier", "")
            item["tier"] = "memoria_validada"
            item["cache_status"] = cache["status"]
            item["searched_at"] = cache["searched_at"]
            item["checked_at"] = cache["checked_at"]
            item["cached_query"] = cache["cached_query"]
        result.append(item)
    return result


def _record_tier_health(tier_name, success, outcome=None):
    try:
        web_search_health.record(tier_name, success, outcome)
    except Exception:
        pass  # telemetria é best-effort — nunca pode derrubar uma busca já resolvida


def _record_tier_failure(tier_name, exc):
    """Health entry for a failed tier call, keeping a 429, a captcha and a quota pause apart from other errors."""
    if isinstance(exc, (TierSkipped, browser_service_client.BrowserSkipped)):
        outcome = "paused"
    elif isinstance(exc, WebProviderRateLimited):
        outcome = "rate_limited"
    elif isinstance(exc, browser_service_client.BrowserCaptcha):
        outcome = "captcha"
    else:
        outcome = "failed"
    _record_tier_health(tier_name, False, outcome)


_is_safe_crawl_target = web_fetch.is_safe_target


def _resolve_seed_url(query):
    query = query.strip()
    if _URL_RE.match(query):
        seed_url = query
    else:
        raw_results = _run_search_script(web_search_adapters.NODE_EXECUTABLE, DDG_SEARCH_SCRIPT,
                                         [query, "--num", "1"], WEB_SEARCH_TIER1_TIMEOUT_S)
        if not raw_results:
            raise RuntimeError("Tier 1 returned no results to resolve the seed URL")
        seed_url = raw_results[0].get("url")
        if not seed_url:
            raise RuntimeError("Tier 1 returned a result without a URL")
    if not _is_safe_crawl_target(seed_url):
        raise ValueError("Seed URL rejected: it points to an internal/reserved network destination")
    return seed_url


_SCOPE_SYNTHESIS_SYSTEM_PROMPT = (
    "You are given raw Markdown scraped from one or more web pages, plus the user's "
    "original search query. Synthesize a concise, accurate answer to the query using "
    "ONLY the provided content — never outside knowledge. If the content doesn't answer "
    "the query, say so explicitly. When there is more than one source page, attribute "
    "claims to the specific URL they came from."
)


def _synthesize_deep_content(query, pages, scope_model):
    combined = ""
    sources = []
    for page in pages:
        remaining = DEEP_JOB_SYNTHESIS_MAX_CHARS - len(combined)
        if remaining <= 0:
            break
        chunk = page.get("markdown", "")[:remaining]
        combined += f"\n\n--- Source: {page.get('url', '')} ---\n{chunk}"
        # chars = quanto do chunk de fato entrou no prompt, não o tamanho bruto
        # da página — sources é o que foi realmente considerado na síntese.
        sources.append({"url": page.get("url", ""), "chars": len(chunk)})
    if not scope_model:
        # Sem scope_model configurado: devolve o próprio conteúdo bruto truncado
        # em vez de falhar o job inteiro — síntese é um refinamento, não um
        # requisito pra o job ter um resultado utilizável.
        return combined[:2000], sources
    token = model_client.get_token()
    response = model_client.fetch(
        "/v1/chat/completions", token, method="POST",
        body={
            "model": scope_model,
            "messages": [
                {"role": "system", "content": _SCOPE_SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"query": query, "content": combined}, ensure_ascii=False)},
            ],
            "temperature": 0,
        },
        # Default de model_client.fetch (15s) é curto pra até 100k chars de
        # contexto — a etapa de crawl já tem orçamento próprio (DEEP_JOB_TIMEOUT_S),
        # a síntese precisa do seu.
        timeout=90,
    )
    summary = response["choices"][0]["message"]["content"]
    return summary, sources


def _run_deep_job(job_id, query, scope):
    # Roda num worker de background (DEEP_JOB_EXECUTOR) — precisa cobrir QUALQUER
    # exceção e sempre concluir com _set_job: se o worker morrer sem atualizar o
    # job, ele fica "pending" pra sempre do ponto de vista de web_search_result,
    # sem qualquer forma do cliente saber que o worker já não existe mais.
    try:
        cfg = config.load_config()
        scope_model = cfg.get("scope_model") or cfg.get("router_model")
        seed_url = _resolve_seed_url(query)
        raw = _run_search_script(
            CAMOUFOX_VENV_PYTHON, CRAWL4AI_SEARCH_SCRIPT,
            [seed_url, "--max-pages", str(DEEP_JOB_MAX_PAGES)], DEEP_JOB_TIMEOUT_S,
        )
        pages = raw.get("pages") if isinstance(raw, dict) else None
        if not pages:
            raise RuntimeError("Tier 4 returned no pages")
        summary, sources = _synthesize_deep_content(query, pages, scope_model)
        _set_job(
            job_id, status="done", error=None,
            result={"summary": _sanitize_text(summary), "sources": sources, "seed_url": seed_url},
            created_at=_jobs.get(job_id, {}).get("created_at", time.time()),
        )
    except Exception as exc:
        _set_job(
            job_id, status="error", result=None,
            error=_sanitize_text(f"{type(exc).__name__}: {exc}"),
            created_at=_jobs.get(job_id, {}).get("created_at", time.time()),
        )


# Schemas/prompts validados em .vscode/scripts/_spike-stop-decision-real-summary-test.py
# contra achados reais (não sintéticos) — ver design/planos/
# smart-tool-agente-pesquisa-item1-integracao.md.
_RESEARCH_FINDINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_findings",
        "description": "Summarize what the research so far has found, structured for a downstream sufficiency check.",
        "parameters": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "sources": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "string", "enum": ["alta", "media", "baixa"]},
                            "depth": {"type": "string", "enum": ["snippet", "pagina_completa"]},
                        },
                        "required": ["claim", "sources", "confidence", "depth"],
                    },
                },
                "gaps": {"type": "array", "items": {"type": "string"}, "description": "Aspects of the topic not yet covered."},
                "source_diversity": {
                    "type": "object",
                    "properties": {
                        "independent_domains": {"type": "integer"},
                        "conflicting_claims": {"type": "boolean"},
                    },
                    "required": ["independent_domains", "conflicting_claims"],
                },
            },
            "required": ["findings", "gaps", "source_diversity"],
        },
    },
}

_RESEARCH_DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "Decide if the research loop should continue or stop.",
        "parameters": {
            "type": "object",
            "properties": {
                "continue_research": {"type": "boolean"},
                "reason": {"type": "string", "description": "One sentence justification."},
            },
            "required": ["continue_research", "reason"],
        },
    },
}

_RESEARCH_PLANNER_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_next_query",
        "description": "Propose the next search query to fill the most relevant known gap.",
        "parameters": {
            "type": "object",
            "properties": {
                "next_query": {"type": "string", "description": "A search-engine query, not a restatement of the original topic."},
                "reason": {"type": "string", "description": "Which gap this targets and why."},
            },
            "required": ["next_query", "reason"],
        },
    },
}

_RESEARCHER_SYSTEM_PROMPT = (
    "You are the research step of a web-research loop. You receive raw search-result "
    "snippets (title/url/snippet) for a topic and must synthesize what they show. Call "
    "submit_findings with a structured summary: one entry per distinct claim (not per "
    "source), tagging confidence and whether you only saw a snippet (not the full page). "
    "List real gaps -- aspects of the topic these snippets do not cover at all."
)

_RESEARCH_CRITIC_SYSTEM_PROMPT = (
    "You decide whether a web-research loop should keep searching or stop and synthesize "
    "an answer now. You receive the original topic, the iteration budget used so far, and "
    "a STRUCTURED summary of findings (claims with confidence/depth, known gaps, source "
    "diversity).\n\n"
    "The 'gaps' list is NOT a checklist you must clear before stopping -- the researcher "
    "lists every direction it could still explore, and for any real topic that list is "
    "never empty. Judge each gap by whether it would change the answer to what was "
    "actually asked, not by whether more depth is theoretically available. A gap is worth "
    "another iteration only if: the topic explicitly asked for that aspect, or the "
    "high-confidence findings so far are too thin/contradictory to answer at all. "
    "Exhaustive completeness (primary sources, compliance detail, edge cases nobody asked "
    "about) is never on its own a reason to continue. Call submit_decision."
)

_RESEARCH_PLANNER_SYSTEM_PROMPT = (
    "You plan the next search query for a web-research loop that already ran once and "
    "was told to continue. You receive the original topic, the queries already searched, "
    "and the structured findings/gaps so far. Propose ONE search-engine query targeting "
    "the single gap most relevant to the original topic -- never repeat a query already "
    "searched, and never just restate the original topic verbatim (that would return the "
    "same pages again). Call submit_next_query."
)


def _research_llm_call(model, token, system_prompt, user_content, tool, timeout=30):
    response = model_client.fetch(
        "/v1/chat/completions", token, method="POST",
        body={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": tool["function"]["name"]}},
            "temperature": 0,
        },
        timeout=timeout,
    )
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    return json.loads(tool_call["function"]["arguments"])


def _call_researcher(topic, raw_results, token, model, timeout=30):
    snippets_text = "\n".join(
        f"- {r.get('title', '')} ({r.get('url', '')}): {r.get('snippet', '')}" for r in raw_results
    )
    user_content = f"Topic: {topic}\n\nRaw search snippets:\n{snippets_text}"
    summary = _research_llm_call(model, token, _RESEARCHER_SYSTEM_PROMPT, user_content, _RESEARCH_FINDINGS_TOOL, timeout=timeout)
    diversity = summary.get("source_diversity") if isinstance(summary, dict) else None
    if (not isinstance(summary, dict) or not isinstance(summary.get("findings"), list)
            or not isinstance(summary.get("gaps"), list) or not isinstance(diversity, dict)
            or not isinstance(diversity.get("independent_domains"), int)
            or not isinstance(diversity.get("conflicting_claims"), bool)):
        raise ValueError("Researcher returned incomplete structured findings")
    for item in summary["findings"]:
        if (not isinstance(item, dict) or not isinstance(item.get("claim"), str)
                or not isinstance(item.get("sources"), list)
                or item.get("confidence") not in ("alta", "media", "baixa")
                or item.get("depth") not in ("snippet", "pagina_completa")):
            raise ValueError("Researcher returned a finding outside the schema")
    # O modelo pode citar uma URL real que conhece de fora dos snippets. O agente
    # chamador recebe só estas fontes, então tal claim pareceria verificável sem ser.
    # Preserva os achados sustentados e transforma os demais em gap para o planner.
    observed_urls = {r.get("url") for r in raw_results if isinstance(r, dict) and r.get("url")}
    backed = []
    rejected = 0
    for item in summary["findings"]:
        cited = item["sources"]
        if cited and all(isinstance(url, str) and url in observed_urls for url in cited):
            backed.append(item)
        else:
            rejected += 1
    summary["findings"] = backed
    if rejected:
        summary["gaps"].append(
            f"{rejected} finding(s) cited sources missing from the results; locate those pages before asserting them."
        )
    return summary


def _call_critic(topic, budget_used, findings, token, model, timeout=30):
    user_content = (
        f"Topic: {topic}\nBudget used: {budget_used}\n"
        f"Structured findings so far (JSON): {json.dumps(findings, ensure_ascii=False)}"
    )
    args = _research_llm_call(model, token, _RESEARCH_CRITIC_SYSTEM_PROMPT, user_content, _RESEARCH_DECISION_TOOL, timeout=timeout)
    if (not isinstance(args, dict) or not isinstance(args.get("continue_research"), bool)
            or not isinstance(args.get("reason"), str)):
        raise ValueError("Critic returned an incomplete decision")
    return args["continue_research"], args["reason"]


def _call_planner(topic, queries_tried, findings, token, model, timeout=30):
    user_content = (
        f"Original topic: {topic}\n"
        f"Queries already searched: {json.dumps(queries_tried, ensure_ascii=False)}\n"
        f"Structured findings/gaps so far (JSON): {json.dumps(findings, ensure_ascii=False)}"
    )
    args = _research_llm_call(model, token, _RESEARCH_PLANNER_SYSTEM_PROMPT, user_content, _RESEARCH_PLANNER_TOOL, timeout=timeout)
    next_query = " ".join(str(args.get("next_query") or "").split())
    tried = {" ".join(query.split()).casefold() for query in queries_tried}
    if not next_query or next_query.casefold() in tried:
        raise ValueError("planner did not propose a new sub-search")
    if len(next_query) > WEB_SEARCH_MAX_QUERY_CHARS:
        raise ValueError("planner proposed a sub-search that is too long")
    return next_query, args.get("reason", "")


def _run_research_job(job_id, topic, num, scope=None):
    # Roda em RESEARCH_JOB_EXECUTOR (pool próprio, separado de DEEP_JOB_EXECUTOR e
    # WEB_SEARCH_EXECUTOR) — mesmo contrato de _run_deep_job: qualquer exceção sempre
    # termina em _set_job, nunca deixa o job "pending" pra sempre.
    try:
        started = time.monotonic()
        deadline = started + RESEARCH_JOB_TIMEOUT_S
        # Uma busca com fallback lento não pode consumir o prazo do pesquisador
        # e devolver fontes sem findings. O espaço reservado cobre até duas
        # chamadas LLM (pesquisador + crítico); em testes com prazo curto, usa
        # no máximo metade do orçamento para preservar a verificação existente.
        llm_reserve = min(RESEARCH_LLM_RESERVE_S, RESEARCH_JOB_TIMEOUT_S / 2)
        search_deadline = deadline - llm_reserve
        cfg = config.load_config()
        model = cfg.get("research_model") or cfg.get("router_model")
        if not model:
            raise RuntimeError("No research_model/router_model configured")
        max_iterations = config.clamp_research_max_iterations(cfg.get("research_max_iterations"))
        token = model_client.get_token()
        try:
            cached, topic_vector = _validated_cache_result(
                "research", topic, scope, cfg, token, time.monotonic() + RESEARCH_CACHE_CHECK_S)
        except Exception as exc:
            _log(f"research cache unavailable on this call: {type(exc).__name__}: {exc}")
            cached, topic_vector = None, None
        if cached:
            entry, cache_status = cached
            result = dict(entry["result"])
            result["volatility"] = entry["classification"]
            result["cache"] = _cache_trace(entry, cache_status)
            _set_job(job_id, status="done", error=None, result=result,
                     created_at=_jobs.get(job_id, {}).get("created_at", time.time()))
            return
        deadline = time.monotonic() + RESEARCH_JOB_TIMEOUT_S
        search_deadline = deadline - llm_reserve

        queries_tried = [topic]
        current_query = topic
        site_domains = _site_domains_from_query(topic)
        all_raw = []
        findings = {}
        rounds = []
        # Só pra `research_metrics`: quanto cada rodada acrescenta de fato.
        urls_seen = set()
        round_gains = []
        stopped_by = "budget"

        for i in range(max_iterations):
            if time.monotonic() >= search_deadline:
                if all_raw:
                    stopped_by = "time_budget"
                    break
                raise TimeoutError("search time exhausted before getting sources")
            try:
                search_options = {"deadline": search_deadline}
                if site_domains:
                    search_options["site_domains"] = site_domains
                round_results = _search_web_once(
                    current_query, num, scope, allow_recall=False, **search_options,
                )
                round_results = _with_npm_evidence(current_query, round_results, num)
            except Exception as exc:
                # Fontes já reunidas em rodadas anteriores continuam úteis. Na primeira
                # rodada, sem fonte alguma, o job falha em vez de pedir ao LLM que
                # invente achados a partir de uma lista vazia.
                if not all_raw:
                    raise
                rounds.append({
                    "query": current_query, "results_count": 0,
                    "continue": False, "reason": f"stopped by a search error: {type(exc).__name__}: {exc}",
                })
                round_gains.append({"new_urls": 0})
                stopped_by = "timeout" if isinstance(exc, TimeoutError) else "error"
                break
            all_raw.extend(round_results)
            round_urls = {r.get("url") for r in round_results if r.get("url")}
            gain = {"new_urls": len(round_urls - urls_seen)}
            urls_seen |= round_urls
            round_gains.append(gain)

            # Pesquisador/crítico falhando (timeout, resposta sem tool-call, etc.) não pode
            # descartar os resultados de busca já acumulados em `all_raw` -- sai do loop com
            # o que já tem em vez de derrubar o job inteiro (`findings` mantém o valor da
            # última rodada bem-sucedida, `all_raw` já foi estendido acima).
            try:
                findings = _call_researcher(
                    topic, all_raw, token, model,
                    timeout=min(RESEARCH_LLM_TIMEOUT_S, _research_time_left(deadline)),
                )
                if isinstance(findings, dict):
                    gain["findings"] = len(findings.get("findings") or [])
                    gain["gaps"] = len(findings.get("gaps") or [])
                budget_used = f"{i + 1}/{max_iterations} iterations"
                continue_research, reason = _call_critic(
                    topic, budget_used, findings, token, model,
                    timeout=min(RESEARCH_LLM_TIMEOUT_S, _research_time_left(deadline)),
                )
            except Exception as exc:
                rounds.append({
                    "query": current_query, "results_count": len(round_results),
                    "continue": False, "reason": f"stopped by an error in the round: {type(exc).__name__}: {exc}",
                })
                stopped_by = "timeout" if isinstance(exc, TimeoutError) else "error"
                break

            rounds.append({
                "query": current_query, "results_count": len(round_results),
                "continue": continue_research, "reason": reason,
            })

            if not continue_research:
                stopped_by = "critic"
                break
            if i == max_iterations - 1:
                break
            if time.monotonic() >= search_deadline:
                stopped_by = "time_budget"
                break

            try:
                planned_query, _planner_reason = _call_planner(
                    topic, queries_tried, findings, token, model,
                    timeout=min(RESEARCH_LLM_TIMEOUT_S, _research_time_left(deadline)),
                )
                current_query = _with_site_operator(planned_query, site_domains)
                if len(current_query) > WEB_SEARCH_MAX_QUERY_CHARS:
                    raise ValueError("planner proposed a query that is too long after applying the domain")
                if " ".join(current_query.split()).casefold() in {
                    " ".join(old.split()).casefold() for old in queries_tried
                }:
                    raise ValueError("planner repeated a query after applying the requested domain")
            except Exception as exc:
                stopped_by = "timeout" if isinstance(exc, TimeoutError) else "planner_error"
                break  # sem próxima query -- fica com os findings/fontes já reunidos
            queries_tried.append(current_query)

        seen_urls = set()
        sources = []
        for r in all_raw:
            url = r.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            sources.append(r)

        research_metrics.log_research_job(
            topic, max_iterations,
            [{**r, **g} for r, g in zip(rounds, round_gains)],
            stopped_by, len(sources), time.monotonic() - started,
        )
        result = {
            "findings": findings, "sources": sources, "rounds": rounds,
            "partial": stopped_by in ("error", "timeout", "planner_error", "time_budget"),
            "stop_reason": stopped_by,
        }
        if not result["partial"] and findings.get("findings"):
            _store_fresh_research("research", topic, scope, result, sources, cfg, topic_vector)
        result["cache"] = {"status": "fresh"}
        _set_job(
            job_id, status="done", error=None,
            result=result,
            created_at=_jobs.get(job_id, {}).get("created_at", time.time()),
        )
    except Exception as exc:
        _set_job(
            job_id, status="error", result=None,
            error=_sanitize_text(f"{type(exc).__name__}: {exc}"),
            created_at=_jobs.get(job_id, {}).get("created_at", time.time()),
        )


def _handle_web_search_result(arguments):
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("web_search_result requires 'job_id'")
    job = _jobs.get(job_id)
    if job is None:
        raise ValueError(f"Unknown job_id: {job_id}")
    payload = {"status": job["status"]}
    if job["status"] == "done":
        payload["result"] = job["result"]
    elif job["status"] == "error":
        payload["error"] = job["error"]
    return _to_json_sanitized(payload)


def _tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


WEB_FETCH_TIMEOUT_S = 90
WEB_FETCH_RENDER_TIMEOUT_S = 60
MOLI_RENDER_TIMEOUT_S = 25
# Engine of the last render on this request's thread: web_fetch.read_page calls _render_page on the handler's thread.
_RENDER = threading.local()
WEB_FETCH_VECTOR_ROOT = os.path.join(paths.DATA_DIR, "web-fetch")


def _moli_markdown(url, timeout):
    """Markdown of a JavaScript page rendered by Moli (layout and paint only when the page needs them). Every request
    Moli makes (redirects, scripts, frames, fetch) refuses private, loopback, link-local and CGNAT addresses, as the
    HTTP reader and the Crawl4AI route guard do: a public page cannot read this machine or its network through it."""
    if not install_runtime.MOLI_EXE.is_file():
        raise RuntimeError(f"Moli is not installed ({install_runtime.MOLI_EXE}); reinstall Smart Tool.")
    done = subprocess.run([str(install_runtime.MOLI_EXE), "fetch", "--block-private-networks", "--dump", "markdown",
                           url], capture_output=True,
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode:
        raise RuntimeError(f"moli exited {done.returncode}: {done.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return done.stdout.decode("utf-8", "replace")


def _render_page(url, deadline):
    """Moli first; Chromium through Crawl4AI only when Moli fails or comes back thin. Measured on 2026-10-10 over 21
    pages HTTP could not read: Moli 1.6 s and 90 MB peak (median), Chromium 4.2 s and 581 MB; Moli read 14, Chromium
    16, both together 17 (only Chromium read YouTube, Google Maps and Airbnb). The engine that answered goes to the
    web_fetch metric through _RENDER."""
    left = deadline - time.monotonic()
    if left < 5:
        return ""
    _RENDER.engine, _RENDER.moli_error = None, None
    try:
        markdown = _moli_markdown(url, min(MOLI_RENDER_TIMEOUT_S, left - 2))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        markdown, _RENDER.moli_error = "", f"{type(exc).__name__}: {exc}"[:200]
        _log(f"web_fetch Moli render failed, trying Chromium: {_RENDER.moli_error}")
    if not web_fetch.thin(markdown):
        _RENDER.engine = "moli"
        return markdown
    left = deadline - time.monotonic()
    if left < 5:
        return markdown
    raw = _run_search_script(CAMOUFOX_VENV_PYTHON, CRAWL4AI_SEARCH_SCRIPT, [url, "--max-pages", "1"],
                             min(WEB_FETCH_RENDER_TIMEOUT_S, left))
    pages = raw.get("pages") if isinstance(raw, dict) else None
    chromium = (pages[0].get("markdown") or "") if pages else ""
    if len(chromium.strip()) > len(markdown.strip()):
        _RENDER.engine = "chromium"
        return chromium
    _RENDER.engine = "moli"
    return markdown


def _web_fetch_embed(texts, deadline):
    """Vectors for the pieces of a long page and the prompt, with the pieces cached per embedding model. Fails loudly
    without an embedding model: a long page is never cut back to its start in silence."""
    model = local_embedder.resolve(config.load_config().get("embedding_model"), None)
    if not model or model == indexer.LEXICAL_MODEL:
        raise RuntimeError("This page is longer than 60,000 characters and choosing its relevant parts needs an "
                           "embedding_model, which is not configured")
    try:
        token = model_client.get_token()
        probe = _embed(model, texts[-1:], token, deadline=deadline)
        cache = embedding_cache.VectorCache(WEB_FETCH_VECTOR_ROOT, indexer.INDEX_DIR, {
            "purpose": "web_fetch_pieces", "model": model, "dimensions": len(probe[0])})
        try:
            pieces = cache.embed(texts[:-1], lambda batch: _embed(model, batch, token, deadline=deadline),
                                 indexer.validate_vector, split=lambda unique: list(_embed_batches(unique)),
                                 workers=indexer.EMBED_WORKERS)
        finally:
            cache.db.close()
    except Exception as exc:
        raise RuntimeError(f"This page is longer than 60,000 characters and the embedding model ({model}) that picks "
                           f"its relevant parts failed: {type(exc).__name__}: {exc}"[:400]) from None
    return pieces + probe


def _web_fetch_unavailable():
    cfg = config.load_config()
    if not cfg.get("router_model"):
        return "no router_model configured"
    return _gateway_problem()


def _handle_web_fetch(arguments):
    url, prompt = arguments.get("url"), arguments.get("prompt")
    started = time.monotonic()
    try:
        if not isinstance(url, str) or not url.strip() or len(url) > 2000:
            raise ValueError("web_fetch requires 'url' (at most 2000 characters)")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise ValueError("web_fetch requires 'prompt' (at most 4000 characters)")
        problem = _web_fetch_unavailable()
        if problem:
            raise RuntimeError(f"Gateway model unavailable ({problem}).")
        deadline = started + WEB_FETCH_TIMEOUT_S
        _RENDER.engine, _RENDER.moli_error = None, None
        page = web_fetch.read_page(url, deadline, _render_page)
        selected, pieces = web_fetch.select(page["text"], prompt, lambda texts: _web_fetch_embed(texts, deadline))
        text = web_fetch.answer(config.load_config()["router_model"], prompt, {**page, "text": selected}, deadline)
    except Exception as exc:
        web_fetch.mark_failed(str(url))
        _metric("web_fetch", error=type(exc).__name__, elapsed_s=round(time.monotonic() - started, 2))
        raise RuntimeError(f"web_fetch failed: {str(exc).rstrip('.')}. The native WebFetch is allowed for this URL for "
                           f"{web_fetch.FAILED_TTL_S // 60} min.") from None
    _metric("web_fetch", cached=page["cached"], rendered=page["rendered"], render_error=page.get("render_error"),
            render_engine=_RENDER.engine if page["rendered"] else None, moli_error=_RENDER.moli_error,
            page_chars=page["chars"], read_chars=len(selected), pieces=pieces,
            answer_chars=len(text), elapsed_s=round(time.monotonic() - started, 2))
    origin = "from cache" if page["cached"] else "rendered in the browser" if page["rendered"] else "downloaded now"
    read_at = datetime.datetime.fromtimestamp(page["fetched_at"]).strftime("%Y-%m-%d %H:%M")
    cut = (f"; page of {page['chars']:,} characters: the {pieces} parts most related to the request were read"
           if pieces else "")
    if page["truncated"]:
        cut += f"; only the first {web_fetch.PAGE_STORE_MAX_CHARS:,} characters were kept"
    return _sanitize_text(f"Source: {page['final_url']} (read at {read_at}, {origin}{cut})\n\n{text}")


def _web_route(tool, url):
    """Hook decision for native WebSearch/WebFetch: redirect to the Smart Tool only while it can answer."""
    if tool not in ("WebSearch", "WebFetch"):
        return {"redirect": False, "reason": "tool not redirected"}
    if tool == "WebFetch" and web_fetch.recently_failed(url):
        return {"redirect": False, "reason": "web_fetch failed on this URL recently"}
    if tool == "WebSearch" and web_fetch.recently_failed(WEB_SEARCH_FAILED_KEY):
        return {"redirect": False, "reason": "web_search failed or came back empty recently"}
    problem = _web_fetch_unavailable()
    if problem:
        return {"redirect": False, "reason": f"gateway unavailable: {problem}"}
    if tool == "WebFetch":
        return {"redirect": True, "reason": "the mini model reads the page, the answer comes back short and the page is "
                "cached for 24 h."}
    return {"redirect": True, "reason": "several sources, two languages and a cache shared across sessions."}


HOOK_MAX_BODY_BYTES = 4 * 1024 * 1024


def _pretool_hook(handler):
    """PreToolUse decision for the hook (Claude Code `type: "http"`, Codex via the thin command). Always 200 with `{}`
    or `deny`: a non-2xx answer would show as a hook error on every tool call, and `allow` would skip the user's
    permission prompt."""
    client = (urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query).get("client") or [""])[0]
    try:
        payload = json.loads(handler._read_body(HOOK_MAX_BODY_BYTES) or b"{}")
        output = hook_decision.decide(payload, client or None, _web_route)
    except Exception as exc:
        _log(f"PreToolUse hook without a decision: {type(exc).__name__}: {exc}")
        return {}
    specific = output.get("hookSpecificOutput") or {}
    if specific.get("permissionDecision", "deny") != "deny":
        return {}
    return output


_TOOL_HANDLERS = {
    "web_fetch": _handle_web_fetch,
    "project_manage": _handle_project_manage,
    "smart_search": _handle_smart_search,
    "smart_search_result": _handle_smart_search_result,
    "web_search": _handle_web_search,
    "web_search_result": _handle_web_search_result,
}


_MCP_SESSION = contextvars.ContextVar("mcp_session", default=None)


def _session_client():
    session = _sessions.get(_MCP_SESSION.get() or "")
    return session, (session or {}).get("client") or {}


def _metric(event, **fields):
    _session, client = _session_client()
    try:
        integration_metrics.record(event, client=client_hooks.identify(client) or client.get("name"),
                                   client_version=client.get("version"), **fields)
    except (OSError, ValueError) as exc:
        _log(f"integration metric not saved: {exc}")


def _dispatch_tool_call(params, session_id=None):
    if not isinstance(params, dict):
        return _tool_result("invalid params: expected an object", is_error=True)
    name = params.get("name")
    arguments = params.get("arguments") or {}
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _tool_result(_sanitize_text(f"Unknown tool: {name}"), is_error=True)
    token = _MCP_SESSION.set(session_id)
    try:
        return _tool_result(handler(arguments))
    except Exception as exc:
        # Boundary central pra qualquer handler atual/futuro: uma exceção pode
        # carregar texto de uma fonte não totalmente confiável (ex.: corpo/reason
        # de erro de um proxy externo) até este ponto sem ter passado por
        # sanitização própria do handler.
        return _tool_result(_sanitize_text(str(exc)), is_error=True)
    finally:
        _MCP_SESSION.reset(token)


def _dispatch(method, params, session_id):
    if method == "initialize":
        client = (params or {}).get("clientInfo") if isinstance((params or {}).get("clientInfo"), dict) else {}
        _sessions[session_id] = {"initialized": True, "client": client}
        try:
            integration_metrics.record("session", client=client_hooks.identify(client) or client.get("name"),
                                       client_version=client.get("version"), client_name=client.get("name"))
        except (OSError, ValueError) as exc:
            _log(f"integration metric not saved: {exc}")
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "smart-tool", "version": version.VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return _dispatch_tool_call(params or {}, session_id)
    if method == "ping":
        return {}
    raise KeyError(method)


_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _loopback_host(header_value, require_port=False):
    """`Host`/`Origin` apontando de fato para o loopback nesta porta.

    Sem isso o daemon é vulnerável a DNS rebinding: a MESMA origem web (um domínio do
    atacante que resolve para 127.0.0.1 depois do primeiro acesso) passa a falar com
    este servidor pelo navegador da vítima, com a política de mesma origem satisfeita —
    e aqui isso significa rodar `smart_search` sobre qualquer diretório da máquina e ler
    o `SETUP_TOKEN` de `GET /setup`. A validação exigida pela spec do MCP para
    transporte local é justamente esta: o navegador não pode falsificar `Host`/`Origin`,
    então recusar tudo que não seja o nome do loopback fecha o vetor.
    """
    if not header_value:
        return False
    netloc = header_value.strip()
    if "://" in netloc:
        netloc = urllib.parse.urlsplit(netloc).netloc
    if netloc.startswith("["):
        host, sep, rest = netloc[1:].partition("]")
        if not sep:
            return False
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port = netloc.rpartition(":")
        if not sep:
            host, port = netloc, ""
    if host.lower() not in _LOOPBACK_HOSTNAMES:
        return False
    if port == str(_BOUND_PORT):
        return True
    # `Host` sem porta é a forma legítima de um cliente que fala na porta padrão; já um
    # `Origin` sem porta é outra origem (porta 80), e qualquer serviço local ali — app
    # corporativo com XSS, servidor de dev — não pode ser tratado como esta página.
    return not port and not require_port


_SETUP_SAVES = {
    "/setup/models": setup_ui.handle_models_save,
    "/setup/providers": setup_ui.handle_providers_save,
    "/setup/local-models": setup_ui.handle_local_models_save,
    "/setup/integration": setup_ui.handle_integration_save,
    "/setup/gateway": setup_ui.handle_gateway_save,
    "/setup/autostart": setup_ui.handle_autostart_action,
    "/setup/contact": setup_ui.handle_contact_save,
}


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "SmartToolDaemon/0.1"

    def log_message(self, fmt, *args):
        pass

    def _origin_allowed(self):
        """`Origin` ausente é permitido de propósito: cliente MCP nativo (Claude Code,
        Cursor, VS Code) não manda o header, e nenhum navegador omite `Origin` em
        requisição cross-origin com efeito colateral."""
        if not _loopback_host(self.headers.get("Host")):
            return False
        origin = self.headers.get("Origin")
        return origin is None or _loopback_host(origin, require_port=True)

    def _reject_foreign_origin(self):
        """The agent's browser (Playwright MCP marks every request with AGENT_HEADER) never reaches the daemon: a page
        it opens could otherwise steer the agent into /setup, whose token unlocks every setting."""
        if self.headers.get(browser_control.AGENT_HEADER):
            self._write_json(403, {"error": "Smart Tool's pages are not available to the agent's browser"})
            return True
        if self._origin_allowed():
            return False
        self._write_json(403, {"error": "origin not allowed"})
        return True

    def do_OPTIONS(self):
        # Nenhum CORS: o único cliente de navegador legítimo é a própria página /setup,
        # que é mesma origem. Responder sem `Access-Control-Allow-*` faz o preflight de
        # qualquer outra origem falhar no navegador.
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _write_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass  # O cliente pode fechar a página durante uma operação assíncrona.

    def _write_html(self, status, html):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # A página carrega o SETUP_TOKEN embutido: não pode ficar em cache de disco nem
        # ser embutida em iframe de outra origem (clickjacking sobre os botões de
        # escrita). A CSP fecha o resto — nenhum recurso externo, e `frame-ancestors`
        # cobre navegadores que já ignoram `X-Frame-Options`; o `unsafe-inline` é
        # necessário porque o <script>/<style> da página são inline por design (sem
        # nenhum arquivo estático servido pelo daemon).
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def _read_body(self, max_bytes):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if length < 0:
            raise ValueError("Invalid Content-Length")
        if length > max_bytes:
            raise RequestTooLarge(f"Request body over {max_bytes} bytes")
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        if self._reject_foreign_origin():
            return
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/status":
            self._write_json(200, {
                "capabilities": (_capabilities_cache.to_dict() if _capabilities_cache else None),
                "sessions": len(_sessions),
            })
            return
        if self.path == "/setup":
            self._write_html(200, setup_ui.render_setup_page())
            return
        if self.path == "/setup/status":
            self._write_json(200, setup_ui.status_payload())
            return
        if self.path == "/setup/models":
            try:
                self._write_json(200, setup_ui.handle_models_get())
            except Exception as exc:
                self._write_json(500, {"error": _sanitize_text(str(exc))})
            return
        if self.path == "/projects":
            import projects_ui
            self._write_html(200, projects_ui.render_projects_page())
            return
        if self.path == "/maps":
            import projects_ui
            self._write_html(200, projects_ui.render_map_window())
            return
        if self.path == "/setup/projects":
            try:
                setup_ui.require_token(self.headers)
                self._write_json(200, _project_action({"action": "list", "full": True}))
            except setup_ui.SetupError as exc:
                self._write_json(403, {"error": str(exc)})
            except Exception as exc:
                self._write_json(500, {"error": _project_error(exc)})
            return
        if self.path == "/setup/integration":
            try:
                self._write_json(200, setup_ui.handle_integration_get())
            except Exception as exc:
                self._write_json(500, {"error": _sanitize_text(str(exc))})
            return
        if self.path == "/setup/local-models":
            try:
                self._write_json(200, setup_ui.handle_local_models_get())
            except Exception as exc:
                self._write_json(500, {"error": _sanitize_text(str(exc))})
            return
        if self.path == "/setup/providers":
            try:
                self._write_json(200, setup_ui.handle_providers_get())
            except Exception as exc:
                self._write_json(500, {"error": _sanitize_text(str(exc))})
            return
        if self.path == "/setup/autostart":
            try:
                self._write_json(200, setup_ui.handle_autostart_get())
            except Exception as exc:
                self._write_json(500, {"error": _sanitize_text(str(exc))})
            return
        self._write_json(404, {"error": "not found"})

    def _post_setup(self, save):
        try:
            setup_ui.require_token(self.headers)
            result = save(json.loads(self._read_body(MCP_MAX_REQUEST_BYTES) or b"{}"))
        except (setup_ui.SetupError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": _sanitize_text(str(exc))})
            return
        except Exception as exc:
            self._write_json(500, {"error": _sanitize_text(str(exc))})
            return
        self._write_json(200, result)

    def do_POST(self):
        if self._reject_foreign_origin():
            return
        if self.path == "/setup/projects":
            try:
                setup_ui.require_token(self.headers)
                payload = json.loads(self._read_body(128 * 1024) or b"{}")
                result = _project_action(payload)
            except setup_ui.SetupError as exc:
                self._write_json(403, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._write_json(400, {"error": _sanitize_text(str(exc))})
            except Exception as exc:
                self._write_json(500, {"error": _project_error(exc)})
            else:
                self._write_json(200, result)
            return
        if urllib.parse.urlsplit(self.path).path == "/hooks/pretool":
            self._write_json(200, _pretool_hook(self))
            return
        save = _SETUP_SAVES.get(self.path)
        if save:
            self._post_setup(save)
            return
        if self.path != "/mcp":
            self._write_json(404, {"error": "not found"})
            return
        # `<form>` de outra origem só consegue enviar urlencoded/multipart/text-plain e
        # não é barrado por preflight: exigir JSON deixa esse caminho inutilizável mesmo
        # que algum navegador entregue a requisição sem `Origin`.
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            self._write_json(415, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Content-Type must be application/json"}})
            return
        try:
            raw = self._read_body(MCP_MAX_REQUEST_BYTES)
        except ValueError as exc:
            status = 413 if isinstance(exc, RequestTooLarge) else 400
            self._write_json(status, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(exc)}})
            return
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._write_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return
        if not isinstance(request, dict):
            self._write_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request: expected a JSON object"}})
            return

        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params")
        session_id = self.headers.get("Mcp-Session-Id") or uuid.uuid4().hex

        if req_id is None:
            self.send_response(202)
            self.end_headers()
            return

        try:
            result = _dispatch(method, params, session_id)
        except KeyError:
            self._write_json(200, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not supported: {method}"}})
            return
        except Exception as exc:
            self._write_json(200, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": _sanitize_text(str(exc))}})
            return

        headers = {"Mcp-Session-Id": session_id} if method == "initialize" else {}
        self._write_json(200, {"jsonrpc": "2.0", "id": req_id, "result": result}, headers)


def _write_registry(port):
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump({"url": f"http://127.0.0.1:{port}", "pid": os.getpid(), "port": port}, f)


def _remove_registry():
    try:
        os.remove(REGISTRY_PATH)
    except OSError:
        pass


class _ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    # allow_reuse_address=True (default) deixa um segundo processo dar bind na
    # mesma porta no Windows mesmo com o primeiro ainda em LISTEN — os dois
    # respondem requisições de forma não-determinística, sem erro visível.
    # False força o bind duplicado a falhar alto (OSError) em vez de silencioso.
    allow_reuse_address = False


def _smart_tool_answers(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return json.loads(response.read()).get("status") == "ok"
    except (OSError, ValueError):
        return False


def _bind_server():
    """Default port, then the port clients already know, then any free port (announced in daemon.json and synced to
    the clients by endpoint_sync). A port held by another live Smart Tool stops this start instead of duplicating it."""
    for port in dict.fromkeys(p for p in (DEFAULT_PORT, endpoint_sync.known_port(), 0) if p is not None):
        try:
            return _ExclusiveThreadingHTTPServer(("127.0.0.1", port), MCPHandler)
        except OSError:
            if port and _smart_tool_answers(port):
                raise SystemExit(f"Smart Tool is already running on port {port}.")
    raise OSError("No local port available for the daemon.")


def _probe_capabilities():
    global _capabilities_cache
    try:
        _capabilities_cache = capabilities.probe()
    except Exception as exc:
        _log(f"capabilities probe failed: {type(exc).__name__}: {exc}")


def main():
    global _PROJECT_MONITOR
    # O bind vem antes de qualquer I/O de rede: enquanto a porta não abre, o
    # health-check de `daemon_launcher.ensure_daemon_running()` falha, e cada tool call
    # do agente fica esperando esse tempo pra então seguir aberto. `capabilities.probe()`
    # leva segundos (registry + /v1/models) e só alimenta `GET /status`, que já trata
    # cache vazio.
    global _BOUND_PORT
    server = _bind_server()
    _BOUND_PORT = server.server_address[1]
    threading.Thread(target=_probe_capabilities, daemon=True).start()
    _write_registry(_BOUND_PORT)
    atexit.register(_remove_registry)
    try:
        for path in endpoint_sync.sync(f"http://127.0.0.1:{_BOUND_PORT}"):
            _log(f"port {_BOUND_PORT} written to {path}")
    except (OSError, ValueError) as exc:
        _log(f"clients not pointed to port {_BOUND_PORT}: {type(exc).__name__}: {exc}")
    atexit.register(BROWSER_SERVICE.close)
    try:
        recovered = project_store.recover_interrupted()
        project_store.import_known_legacy(indexer.INDEX_DIR)
        _PROJECT_MONITOR = project_monitor.ProjectMonitor(_enqueue_monitored_project)
        _PROJECT_MONITOR.start()
        _log(f"project monitor started; {recovered} interrupted job(s) recovered")
    except Exception as exc:
        _log(f"project monitor unavailable: {_project_error(exc)}")
    try:
        server.serve_forever()
    finally:
        if _PROJECT_MONITOR:
            _PROJECT_MONITOR.stop()
        with _JOBS_LOCK:
            pending = [key for key, value in _jobs.items() if value.get("kind") in _PROJECT_KINDS and value.get("status") == "pending"]
        for job_id in pending:
            _cancel_project_job(job_id)
        SMART_SEARCH_EXECUTOR.shutdown(wait=True)
        server.server_close()
        _remove_registry()


if __name__ == "__main__":
    main()
