#!/usr/bin/env python3
"""Roteador de tool calls do Smart Tool. V1 não tem classificador mecânico de "tem
escopo": toda chamada de tool nativa casada pelo hook passa por aqui, que pergunta a um
modelo pequeno se deve deixar passar ou redirecionar pra `smart_search`. Cada decisão é
logada em `router-metrics.jsonl` pra orientar um classificador mais fino depois, com
base em uso real — não numa suposição a priori.
"""
import contextvars
import json
import os
import paths
import re
import time

import atomic_io
import model_client
import index_scope
import indexer
import web_search_adapters

# Per request: the daemon serves each hook call on its own thread, so a module global mixed up clients.
CLIENT = contextvars.ContextVar("router_client", default=None)
METRICS_PATH = os.path.join(paths.DATA_DIR, "router-metrics.jsonl")

_SYSTEM_PROMPT = (
    "You decide whether a tool call (native: Grep, Glob, Read, or Bash running a "
    "search/read command such as grep/find/cat/ls -R/rg) should execute directly or "
    "be redirected to an indexed semantic search (smart_search), because it is "
    "potentially expensive or imprecise (e.g. no path filter, a large directory like "
    "node_modules, an unscoped recursive command, or a question better answered by "
    "semantic search than by regex/raw listing). For Bash, judge by the command's "
    "content (the tool_input 'command' field): only consider redirecting commands "
    "whose purpose is to search/read source code — never redirect commands with any "
    "other effect (build, git, install, running a script, etc.), even if grep/cat/find "
    "appear mid-pipeline.\n\n"
    "The 'target' object is measured on disk right now — prefer it over any guess you "
    "would make from the path string alone:\n"
    "- target.files / target.truncated: files found under the target, ignoring the "
    "directories listed in target.heavy_dirs and target.ignored_by_default. 'truncated': "
    "true means the scan hit its budget, so the tree is big.\n"
    "- target.heavy_dirs: dependency/build trees present under the target (node_modules, "
    ".venv, dist...). A raw recursive search may reach them, unless the project's ignore "
    "rules already exclude them.\n"
    "- target.ignored_by_default: directories the native search tool skips on its own "
    "(.git, __pycache__, dot-directories). Their presence is NOT a cost signal.\n"
    "- target.scope: present when the target could not be derived from the call itself, "
    "so the measurement describes the session directory and not what the command reads.\n"
    "- target.outside_project: the target is outside the project root; no index covers "
    "it, so never redirect.\n"
    "- target.indexed: whether a semantic index already exists for this project. When it "
    "is false, redirecting forces a cold index build inside the call (embedding every "
    "file, tens of seconds) — only worth it when the search really is broad.\n"
    "- target.missing: true when the path does not exist; never redirect those, the "
    "native tool's own error is the useful answer.\n\n"
    "Reading one specific file is never redirected. When in doubt, answer 'allow'. "
    "Call submit_decision with your answer."
)

# Toda chamada de Grep/Glob/Read/Bash do agente espera por esta decisão, e o desfecho de
# falha é `allow` — então esperar mais só atrasa o agente sem mudar o resultado. O valor
# pressupõe `router_model` sem cadeia de raciocínio: um modelo "thinking" gasta mais de mil
# tokens numa decisão binária e estoura isto sempre, abrindo o breaker e deixando o
# roteamento desligado de forma permanente.
DECISION_TIMEOUT_S = 8

_SCAN_MAX_FILES = 4000
_SCAN_MAX_SECONDS = 0.25
# Percorridos pela tool nativa: a presença deles é sinal de custo de verdade.
_HEAVY_DIR_NAMES = frozenset({
    "node_modules", ".venv", "venv", "vendor", "target", "dist", "build", "out",
    ".next", ".nuxt", ".gradle", "Pods",
})
# Grep/Glob do Claude Code são ripgrep, que já pula estes por padrão — listá-los junto
# dos de cima superestimaria o custo e enviesaria a decisão pra `redirect`.
_IGNORED_BY_DEFAULT_DIRS = frozenset({".git", "__pycache__", ".mypy_cache", ".pytest_cache"})
_PATH_FIELDS = ("path", "file_path", "notebook_path")


def _resolve_path(raw, cwd):
    """Path do `tool_input` normalizado para o disco. Relativo resolve contra o `cwd` do
    payload (não o do processo do hook, que é outro), e a forma MSYS `/c/...` — que o
    agente emite com frequência em shell POSIX no Windows — é convertida, senão
    `os.path.exists` responde `False` sobre um arquivo que existe."""
    if not raw:
        return None
    path = raw.strip().replace("\\", "/")
    msys = re.fullmatch(r"/([A-Za-z])(/.*)?", path)
    if msys:
        path = f"{msys.group(1).upper()}:{msys.group(2) or '/'}"
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _target_path(tool_input, cwd):
    for field in _PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _command_paths(command, cwd):
    """Paths que aparecem como argumento do comando e existem em disco. Sem isto, um
    `grep -n foo arquivo.py` num monorepo seria medido como se varresse o repo todo."""
    found = []
    previous, base = "", cwd
    for token in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', command or ""):
        raw = next((t for t in token if t), "")
        after_cd, previous = previous.lower() in ("cd", "pushd", "set-location", "sl"), raw
        if after_cd:
            # The directory of a `cd` is where the command runs (base for what follows), not what it searches.
            base = _resolve_path(raw, base) or base
            continue
        if not raw or raw.startswith("-"):
            continue
        resolved = _resolve_path(raw, base)
        if resolved and os.path.exists(resolved):
            found.append(resolved)
    return found


def _scan_target(path):
    files = 0
    heavy = set()
    ignored = set()
    truncated = False
    deadline = time.monotonic() + _SCAN_MAX_SECONDS
    for dirpath, dirnames, filenames in os.walk(path):
        for name in list(dirnames):
            if name in _HEAVY_DIR_NAMES:
                heavy.add(name)
                dirnames.remove(name)
            elif name in _IGNORED_BY_DEFAULT_DIRS or name.startswith("."):
                ignored.add(name)
                dirnames.remove(name)
        for _ in filenames:
            files += 1
            # Testar o orçamento só entre diretórios deixaria um único diretório com
            # centenas de milhares de entradas passar por cima dele.
            if files > _SCAN_MAX_FILES or time.monotonic() > deadline:
                return {"files": files, "heavy_dirs": sorted(heavy),
                        "ignored_by_default": sorted(ignored), "truncated": True}
        if time.monotonic() > deadline:
            truncated = True
            break
    return {"files": files, "heavy_dirs": sorted(heavy),
            "ignored_by_default": sorted(ignored), "truncated": truncated}


def _index_facts(project_root):
    try:
        # A identidade é compartilhada; índices legados continuam visíveis até a migração.
        db = indexer.existing_db_path(project_root)
        if db and os.path.isfile(db):
            return {"indexed": True, "index_kib": os.path.getsize(db) // 1024}
        return {"indexed": False}
    except OSError:
        return {"indexed": False}


def _target_facts(tool_name, tool_input, cwd):
    """Fatos medidos em disco sobre o alvo da chamada. Roda antes de toda tool call
    casada pelo hook, então é limitado por tempo e por contagem, não desce em diretório
    pesado e nunca sai da raiz do projeto. Sem isto o modelo julga custo só pelo texto do
    path."""
    raw = _target_path(tool_input, cwd)
    if raw is None and tool_name == "Bash":
        candidates = _command_paths(tool_input.get("command"), cwd)
        if candidates:
            raw = candidates[0]
    scope_note = None
    if raw is None:
        raw, scope_note = cwd, "session cwd, not derived from the command"
    path = _resolve_path(raw, cwd)
    if not path:
        return {}
    facts = {"path": _display_path(path, cwd)}
    if scope_note:
        facts["scope"] = scope_note
    # Fora da raiz do projeto nada é medido: varrer `C:\` ou o perfil do usuário antes de
    # uma tool call custaria mais que a própria chamada, e a decisão seria `allow` igual.
    if cwd and not index_scope._within_root(cwd, path):
        facts["outside_project"] = True
        return facts
    try:
        if os.path.isfile(path):
            facts["is_file"] = True
            facts["kib"] = os.path.getsize(path) // 1024
            return facts
        if not os.path.isdir(path):
            # Só afirma ausência quando o path é inequívoco; adivinhar `missing` faria o
            # pré-filtro liberar a chamada com um motivo falso no dataset de calibração.
            facts["missing"] = True
            return facts
        facts.update(_scan_target(path))
    except OSError as exc:
        facts["scan_error"] = str(exc)
        return facts
    facts.update(_index_facts(cwd or path))
    return facts


def _display_path(path, cwd):
    """Relativo ao projeto quando possível: o path absoluto carrega o nome do usuário do
    Windows e vai inteiro no prompt enviado ao gateway."""
    if cwd:
        try:
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".."):
                return rel.replace(os.sep, "/")
        except ValueError:
            pass
    return os.path.basename(path) or path

_DECISION_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "Submit the routing decision for this tool call.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["allow", "redirect"]},
                "reason": {"type": "string", "description": "Short justification, one sentence."},
            },
            "required": ["decision", "reason"],
        },
    },
}


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
    r"\bgit\s+(grep|ls-files|show|log\s+-p|diff)\b|\b(curl|wget|iwr|invoke-webrequest)\b|\bgh\s+api\b|"
    r"readFileSync|read_text\(|open\(",
    re.IGNORECASE,
)
_READ_TOOLS = frozenset({"Read", "NotebookRead"})

BREAKER_PATH = os.path.join(os.path.dirname(METRICS_PATH), "router-breaker.json")
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 300


def _mechanical_decision(tool_name, tool_input):
    """`(decisão, motivo)` quando a resposta não depende de julgamento nem de tocar o
    disco, ou `None`.

    Medido em 1973 decisões reais: 97,8% eram `allow`, e 1512 delas (77%) caíam nestes
    casos — cada uma custando uma ida ao gateway antes de toda tool call do agente. Os
    dois únicos `Read` redirecionados nesse histórico eram decisões erradas (um deles com
    `offset`/`limit` explícitos, onde só as linhas pedidas servem). Remedido em 2026-10-08
    sobre 2488 Bash que foram ao modelo (mediana 1,5 s, 7,8% redirecionados): ignorar os
    filtros depois de `|` (salvo quando o produtor lê código ou páginas) decide 439 deles
    sem modelo (13,3 min em 58 h) e perde 1 dos 155 redirecionamentos."""
    if tool_name in _READ_TOOLS:
        return "allow", "Reading a specific file is never replaced by semantic search."
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return "allow", "Bash without a readable command."
        if not _SEARCH_VERB_RE.search(command) and not _READ_PRODUCER_RE.search(command):
            return "allow", "The command neither searches nor reads source code."
    return None


def _mechanical_decision_from_facts(facts):
    if facts.get("missing"):
        return "allow", "The path does not exist: the native tool's error is the useful answer."
    if facts.get("is_file"):
        return "allow", "Target is a single file, already scoped."
    if facts.get("outside_project"):
        return "allow", "Target is outside the project root: no index covers it."
    return None


def _breaker_state():
    try:
        with open(BREAKER_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _breaker_write(state):
    # tmp + replace: processos concorrentes gravam aqui (cada tool call é um processo
    # novo), e um `open("w")` truncado no meio faz o leitor ver arquivo vazio — ou seja,
    # breaker fechado justo quando ele deveria estar aberto. Sem `fsync`: perder a última
    # escrita aqui é inofensivo e este caminho roda antes de cada tool call.
    try:
        os.makedirs(os.path.dirname(BREAKER_PATH), exist_ok=True)
        tmp_path = f"{BREAKER_PATH}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp_path, BREAKER_PATH)
    except OSError:
        pass


def breaker_open_until():
    """Instante até quando o roteador está desligado por falhas recentes, ou 0.

    Gateway lento é o pior caso de custo: sem isto, cada tool call paga o timeout inteiro
    para terminar em `allow` de qualquer forma. O estado é compartilhado em arquivo porque
    cada chamada do hook é um processo novo — nada sobrevive em memória."""
    now = time.time()
    try:
        until = float(_breaker_state().get("open_until") or 0)
    except (TypeError, ValueError):
        return 0
    # Teto pelo próprio cooldown: um salto de relógio (resume de VM, correção de fuso) no
    # instante da gravação deixaria um `open_until` no futuro distante, e o roteador
    # ficaria desligado pra sempre — sem erro em lugar nenhum, porque com o breaker aberto
    # nunca há sucesso pra reabrir.
    if until - now > BREAKER_COOLDOWN_S:
        return 0
    return until if now < until else 0


def note_failure():
    state = _breaker_state()
    now = time.time()
    # Janela de tempo em vez de contador: cada hook é um processo, e três falhas
    # simultâneas leriam todas `failures=0` e gravariam `1`, nunca alcançando o limite.
    # Falha de semana passada também não pode somar com a de hoje.
    recent = [t for t in state.get("failures", []) if isinstance(t, (int, float)) and now - t < BREAKER_COOLDOWN_S]
    recent.append(now)
    state["failures"] = recent[-BREAKER_FAILURES:]
    if len(recent) >= BREAKER_FAILURES:
        state["open_until"] = now + BREAKER_COOLDOWN_S
        state["failures"] = []
    _breaker_write(state)


def note_success():
    state = _breaker_state()
    if not state:
        return
    # Um cooldown aberto por outro processo sobrevive: quem leu o estado antes da abertura
    # e teve sucesso não pode apagar a decisão de quem já viu o gateway falhar 3 vezes.
    open_until = breaker_open_until()
    _breaker_write({"open_until": open_until} if open_until else {})


def decide(tool_name, tool_input, router_model, cwd=None):
    safe_input = tool_input if isinstance(tool_input, dict) else {}

    # Ordem importa: tudo que decide sem tocar disco vem antes do snapshot, e o breaker
    # vem antes dele também — em cooldown o desfecho é `allow` de qualquer forma, então
    # percorrer diretório ali seria I/O puro por nada.
    mechanical = _mechanical_decision(tool_name, safe_input)
    if mechanical:
        decision, reason = mechanical
        _log_decision(tool_name, tool_input, decision, reason, extra={"mechanical": True})
        return decision, reason

    if breaker_open_until():
        reason = "Router in cooldown after recent gateway failures."
        _log_decision(tool_name, tool_input, "allow", reason, extra={"breaker": True})
        return "allow", reason

    facts = _target_facts(tool_name, safe_input, cwd)
    mechanical = _mechanical_decision_from_facts(facts)
    if mechanical:
        decision, reason = mechanical
        _log_decision(tool_name, tool_input, decision, reason, extra={"mechanical": True})
        return decision, reason

    started = time.monotonic()
    try:
        # O orçamento inteiro do hook no host é de poucos segundos: obter token não pode
        # consumir mais que a própria decisão.
        token = model_client.get_token()
        response = model_client.fetch(
            "/v1/chat/completions", token, method="POST", timeout=DECISION_TIMEOUT_S,
            body={
                "model": router_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({
                        # Mesma redação do log: a linha de comando também vai pro gateway.
                        "tool_name": tool_name, "tool_input": _redacted_input(safe_input),
                        "target": facts,
                    }, ensure_ascii=False)},
                ],
                "tools": [_DECISION_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "submit_decision"}},
                "temperature": 0,
            },
        )
        tool_call = response["choices"][0]["message"]["tool_calls"][0]
        args = json.loads(tool_call["function"]["arguments"])
    except Exception:
        note_failure()
        raise
    note_success()
    decision = args.get("decision", "allow")
    reason = args.get("reason", "")
    _log_decision(tool_name, tool_input, decision, reason, extra={"ms": round((time.monotonic() - started) * 1000)})
    return decision, reason


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
