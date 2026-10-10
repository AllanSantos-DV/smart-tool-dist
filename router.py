#!/usr/bin/env python3
"""Routing of the native tool calls the PreToolUse hook matches (Grep, Glob, Bash, Read). Deterministic: calls that
neither search nor read code, or that change files, run; accepted block patterns redirect; the rest follows
redirect_rule. Searches the rule lets run are reviewed in the background by the router model, which may propose new
block patterns (block_patterns). Every decision is logged in router-metrics.jsonl. Also holds the router-model
helpers of the web search (tier order and result assessment)."""
import contextvars
import json
import os
import paths
import re
import sys
import time

import atomic_io
import block_patterns
import model_client
import redirect_rule
import web_search_adapters

# Per request: the daemon serves each hook call on its own thread, so a module global mixed up clients.
CLIENT = contextvars.ContextVar("router_client", default=None)
METRICS_PATH = os.path.join(paths.DATA_DIR, "router-metrics.jsonl")

METRICS_MAX_BYTES = 4 * 1024 * 1024
_METRIC_MAX_FIELD_CHARS = 600
_SECRET_ARG_RE = re.compile(
    r"(?i)(authorization[:=]\s*(?:bearer|basic|token)?\s*|bearer\s+|"
    r"--?(?:with-)?token[= ]|api[_-]?key[= ]|password[= ]|passwd[= ]|secret[= ]|-p\s+)"
    r"[^\s\"']+"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")


def _redacted_input(tool_input):
    """Cópia do `tool_input` segura para persistir. O arquivo é append-only, fica em claro
    no perfil do usuário e é legível pelo próprio agente; um `Bash` com credencial na
    linha de comando (`curl -H "Authorization: Bearer ..."`, `psql postgres://u:p@h`) não
    pode virar registro permanente. O campo é truncado porque heredoc inteiro no JSONL
    passa do tamanho em que o append concorrente deixa de ser atômico."""
    if not isinstance(tool_input, dict):
        return tool_input
    safe = {}
    for key, value in tool_input.items():
        if not isinstance(value, str):
            safe[key] = value
            continue
        text = _SECRET_ARG_RE.sub(lambda m: m.group(1) + "***", value)
        text = _URL_CREDENTIAL_RE.sub(lambda m: m.group(1) + "***:***@", text)
        if len(text) > _METRIC_MAX_FIELD_CHARS:
            text = text[:_METRIC_MAX_FIELD_CHARS] + f"...(+{len(text) - _METRIC_MAX_FIELD_CHARS} chars)"
        safe[key] = text
    return safe


def _log_decision(tool_name, tool_input, decision, reason, extra=None):
    record = {
        "ts": time.time(),
        "tool_name": tool_name,
        "tool_input": _redacted_input(tool_input),
        "decision": decision,
        "reason": reason,
        **({"client": CLIENT.get()} if CLIENT.get() else {}),
    }
    record.update(extra or {})
    try:
        os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
        atomic_io.rotate(METRICS_PATH, METRICS_MAX_BYTES)
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Métrica é para calibrar depois, não para a decisão: disco cheio ou arquivo
        # travado não pode derrubar uma tool call do agente.
        pass


def log_unavailable(tool_name, tool_input, exc):
    """Registra o `allow` por indisponibilidade na mesma trilha das decisões reais. Sem
    isto a taxa de falha do roteador é invisível: a métrica só teria as chamadas em que
    o modelo respondeu, e a leitura de `redirect`/`allow` sairia enviesada."""
    _log_decision(
        tool_name, tool_input, "allow", f"{type(exc).__name__}: {exc}",
        extra={"unavailable": True},
    )


# Search/read verbs count only where they start a command: at the beginning, after `;`, `&&`, `||`, `$(`, a backtick,
# a loop/if keyword or a shell invoker (`bash -c`, `powershell -Command`, `cmd /c`). After a single `|` they filter the
# output of the command before them (`python x.py | tail -5`), which reads no source code.
_SEARCH_VERB_RE = re.compile(
    r"(?:^|[;&]|\|\||\$\(|`|-c\s|-Command\s|/c\s|/k\s)\s*[\"']?\s*(?:(?:do|then|else)\s+)?(?:sudo\s+|command\s+)?"
    r"(grep|egrep|fgrep|rg|ripgrep|find|findstr|fd|cat|type|head|tail|less|more|"
    r"ls|dir|awk|sed|ack|ag|select-string|sls|get-content|gc|get-childitem|gci)\b",
    re.IGNORECASE,
)
# Commands that read code or pages without a search verb at their start: their pipe filters still search content.
_READ_PRODUCER_RE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(grep|ls-files|show|log\s+-p|diff)\b|\b(curl|wget|iwr|invoke-webrequest)\b|\bgh\s+api\b|"
    r"readFileSync|read_text\(|open\(",
    re.IGNORECASE,
)
_READ_TOOLS = frozenset({"Read", "NotebookRead"})
# Commands that change files, the repository or dependencies are never redirected (smart_search cannot do the change).
# Verbs are matched outside quoted strings, so a grep pattern like "pip install" is not one; file writes inside an
# inline script (node -e, python -c) are matched anywhere.
_QUOTED_RE = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*\"""")
_MUTATING_VERB_RE = re.compile(
    r"(?:^|[;&|(]|\s)(?:rm|mv|cp|mkdir|rmdir|touch|chmod|tee)\s"
    r"|\bgit\s+(?:-C\s+\S+\s+)?(?:checkout|commit|add|push|pull|reset|stash|restore|rm|mv|merge|rebase|apply|tag|clean|"
    r"clone|switch|cherry-pick)\b|\bsed\s+-i"
    r"|\b(?:npm|pnpm|yarn|pip|uv)\s+(?:install|i|add|run|publish|ci|test)\b"
)
_FILE_WRITE_RE = re.compile(r"writeFileSync|writeFile\(|write_text\(|Set-Content|Out-File")


def _mutates(command):
    """Whether a shell command changes files, the repository or dependencies."""
    return bool(_FILE_WRITE_RE.search(command) or _MUTATING_VERB_RE.search(_QUOTED_RE.sub("''", command)))

def _mechanical_decision(tool_name, tool_input):
    """`(decisão, motivo)` quando a resposta não depende de julgamento nem de tocar o
    disco, ou `None`.

    Medido em 1973 decisões reais: 97,8% eram `allow`, e 1512 delas (77%) caíam nestes
    casos — cada uma custando uma ida ao gateway antes de toda tool call do agente. Os
    dois únicos `Read` redirecionados nesse histórico eram decisões erradas (um deles com
    `offset`/`limit` explícitos, onde só as linhas pedidas servem). Remedido em 2026-10-08
    sobre 2488 Bash que foram ao modelo (mediana 1,5 s, 7,8% redirecionados): ignorar os
    filtros depois de `|` (salvo quando o produtor lê código ou páginas) decide 439 deles
    sem modelo (13,3 min em 58 h) e perde 1 dos 155 redirecionamentos. Remedido em 2026-10-09 sobre 3294 Bash decididos
    pelo modelo: comandos que alteram arquivos, o repositório ou dependências foram 12 dos 283 redirecionamentos (todos
    errados: o agente contornava o bloqueio) e 566 liberações."""
    if tool_name in _READ_TOOLS:
        return "allow", "Reading a specific file is never replaced by semantic search."
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return "allow", "Bash without a readable command."
        if not _SEARCH_VERB_RE.search(command) and not _READ_PRODUCER_RE.search(command):
            return "allow", "The command neither searches nor reads source code."
        if _mutates(command):
            return "allow", "The command changes files, the repository or dependencies."
    return None


def _log_error(message):
    print(f"[smart-tool] {message}", file=sys.stderr, flush=True)


def decide(tool_name, tool_input, cwd=None):
    """(decision, reason, target) for one hook call: decision is redirect or allow, target the folder the redirected
    search would read (the session folder for an accepted pattern, None when allowed). An allowed search is queued for
    the router model's review, which never delays the call."""
    safe_input = tool_input if isinstance(tool_input, dict) else {}
    mechanical = _mechanical_decision(tool_name, safe_input)
    if mechanical:
        decision, reason = mechanical
        _log_decision(tool_name, tool_input, decision, reason, extra={"mechanical": True})
        return decision, reason, None
    learned = block_patterns.matching(tool_name, safe_input)
    if learned:
        reason = f"accepted block pattern: {learned['reason']}"
        _log_decision(tool_name, tool_input, "redirect", reason, extra={"pattern": learned["id"]})
        return "redirect", reason, cwd
    decision, target, reason = redirect_rule.decide(tool_name, safe_input, cwd)
    _log_decision(tool_name, tool_input, decision, reason, extra={"rule": True})
    if decision == "allow":
        try:
            block_patterns.review_later(tool_name, safe_input, reason, METRICS_PATH, _redacted_input, _log_error)
        except (OSError, ValueError) as exc:
            _log_error(f"block pattern review not queued: {exc}")
    return decision, reason, target


def _web_search_tier_system_prompt():
    # Montado a partir de web_search_adapters.ADAPTERS (campo "hint") em vez de fixo:
    # um adaptador novo/removido passa a valer aqui sem editar este arquivo.
    lines = [
        "You choose which tier to try FIRST for a web search request, given the query, an "
        "optional scope hint, and the recent success rate of each tier (null means no data "
        "yet, treat as neutral). Tiers, in default preference order:",
    ]
    for adapter in web_search_adapters.ADAPTERS:
        lines.append(f"- '{adapter['name']}': {adapter['hint']}")
    lines.append(
        "Prefer the default order unless a later tier's recent success rate is clearly "
        "better than an earlier one's. Call submit_tier."
    )
    return "\n".join(lines)


def _tier_decision_tool():
    return {
        "type": "function",
        "function": {
            "name": "submit_tier",
            "description": "Submit which tier to try first for this web search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tier": {"type": "string", "enum": web_search_adapters.DEFAULT_TIER_ORDER},
                    "reason": {"type": "string", "description": "Short justification, one sentence."},
                },
                "required": ["tier", "reason"],
            },
        },
    }


def decide_web_search_tier(query, scope, health_summary, router_model):
    token = model_client.get_token()
    user_content = json.dumps(
        {"query": query, "scope": scope, "recent_success_rate": health_summary},
        ensure_ascii=False,
    )
    response = model_client.fetch(
        "/v1/chat/completions", token, method="POST",
        body={
            "model": router_model,
            "messages": [
                {"role": "system", "content": _web_search_tier_system_prompt()},
                {"role": "user", "content": user_content},
            ],
            "tools": [_tier_decision_tool()],
            "tool_choice": {"type": "function", "function": {"name": "submit_tier"}},
            "temperature": 0,
        },
    )
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(tool_call["function"]["arguments"])
    tier = args.get("tier", web_search_adapters.DEFAULT_TIER_ORDER[0])
    reason = args.get("reason", "")
    return tier, reason


_SEARCH_SUFFICIENCY_TOOL = {
    "type": "function",
    "function": {
        "name": "assess_search",
        "description": "Decide if additional search providers are needed for this query.",
        "parameters": {
            "type": "object",
            "properties": {
                "need_more": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["need_more", "reason"],
        },
    },
}


def assess_web_search_results(query, results, requested_num, scope, model, timeout=12):
    """Uma decisão curta sobre relevância/cobertura antes do fallback caro."""
    token = model_client.get_token()
    evidence = [
        {"title": str(row.get("title") or "")[:180],
         "url": str(row.get("url") or "")[:240],
         "snippet": str(row.get("snippet") or "")[:420]}
        for row in results[:min(requested_num, 10)]
    ]
    response = model_client.fetch(
        "/v1/chat/completions", token, method="POST", timeout=timeout,
        body={
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "You assess whether a QUICK web search has enough relevant sources to answer "
                    "the user's actual query. Call assess_search. Set need_more=true only when "
                    "the central requested fact or aspect is absent, results are mostly off-topic "
                    "or duplicative, or time-sensitive results are clearly stale. Do not demand "
                    "exhaustive depth or primary sources unless the query asks for them. A few "
                    "good snippets can be sufficient; the calling agent writes the final answer."
                )},
                {"role": "user", "content": json.dumps({
                    "query": query, "scope": scope, "requested_results": requested_num,
                    "results": evidence,
                }, ensure_ascii=False)},
            ],
            "tools": [_SEARCH_SUFFICIENCY_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "assess_search"}},
            "temperature": 0,
        },
    )
    call = response["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    if not isinstance(args.get("need_more"), bool):
        raise ValueError("Search evaluation did not return a boolean need_more.")
    return args["need_more"], str(args.get("reason") or "")
