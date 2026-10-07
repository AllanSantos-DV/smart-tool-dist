#!/usr/bin/env python3
"""Tela de modelos do Smart Tool, servida pelo daemon em `GET /setup`.

Esta tela aponta o gateway de modelos OpenAI-compatível (URL base + API key opcional),
escolhe modelos, controla o autostart e recebe chaves opcionais de Tavily/Firecrawl,
protegidas por DPAPI no config próprio do Smart Tool.

`SETUP_TOKEN` é gerado uma vez por processo (nunca persistido em disco) e embutido no
HTML de `GET /setup`; as rotas que escrevem estado (`POST /setup/models`, `POST /setup/gateway`,
`POST /setup/providers` e `POST /setup/autostart`) exigem
esse token de volta no header `X-Setup-Token`. Não é uma
trava contra quem já tem acesso de leitura à máquina (que também leria o token de
`GET /setup`) — só impede escrever config sem nunca ter carregado a página.
"""
import html
import json
import os
import re
import secrets

import autostart
import client_hooks
import config
import daemon_launcher
import endpoint_sync
import gateway
import model_catalog
import model_defaults
import local_embedder
import secret_store
import version
import web_search_external

SETUP_TOKEN = secrets.token_urlsafe(24)

AGENT_CLIENTS = {"claude": {"name": "claude-code"}, "codex": {"name": "codex"}}


def setup_origin():
    """The port the daemon announced in daemon.json (it may fall back from 8765 when another program holds it)."""
    registry = daemon_launcher.read_registry()
    return registry["url"] if registry else f"http://127.0.0.1:{os.environ.get('SMART_TOOL_PORT', '8765')}"


def gateway_state():
    return {**gateway.describe(), **gateway.status()}


def handle_gateway_save(body):
    """{"adapter": name ("" disconnects), "values": {field: value}}; a secret field absent keeps, "" removes."""
    if not isinstance(body, dict) or not isinstance(body.get("adapter", ""), str) \
            or not isinstance(body.get("values", {}), dict):
        raise SetupError("Send the adapter and the field values in a JSON object.")
    try:
        if body.get("remove_secret"):
            gateway.remove_secret(body.get("adapter", ""), body["remove_secret"])
        else:
            gateway.save(body.get("adapter", ""), body.get("values") or {})
    except (ValueError, RuntimeError, OSError, config.ConfigCorruptedError) as exc:
        raise SetupError(str(exc))
    return {"ok": True, "gateway": gateway_state()}


def status_payload():
    gateway = gateway_state()
    models = model_defaults.ensure_recommended_models(gateway["status"] == "ready")
    cfg = config.load_config()
    return {
        "gateway": gateway,
        "models_status": models["status"],
        "models_message": models["message"],
        "missing_models": models["missing"],
        "router_model": cfg.get("router_model", ""),
        "scope_model": cfg.get("scope_model", ""),
        "research_model": cfg.get("research_model", ""),
        "research_max_iterations": cfg.get("research_max_iterations", config.DEFAULT_CONFIG["research_max_iterations"]),
        "quick_search_timeout_s": cfg.get("quick_search_timeout_s", config.DEFAULT_CONFIG["quick_search_timeout_s"]),
        "site_memory_similarity_threshold": cfg.get("site_memory_similarity_threshold", config.DEFAULT_CONFIG["site_memory_similarity_threshold"]),
        "embedding_model": cfg.get("embedding_model", ""),
        "rerank_model": cfg.get("rerank_model", ""),
    }


def render_setup_page():
    # Defesa em profundidade: um valor com esse literal fecharia a tag <script> antes da hora.
    status_json = json.dumps(status_payload(), ensure_ascii=False).replace("</", "<\\/")
    replacements = {
        "__STATUS_JSON__": status_json,
        "__SETUP_TOKEN__": json.dumps(SETUP_TOKEN),
        # Único valor que entra no corpo do HTML, e não no <script>: escapado porque vem
        # de variável de ambiente, ainda que só quem já executa código possa definí-la.
        "__SETUP_ORIGIN__": html.escape(setup_origin(), quote=True),
    }
    # Um único passe sobre o template original: .replace() encadeado re-escanearia o
    # próprio resultado e corromperia o JSON se um valor coincidisse com o outro placeholder.
    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    return pattern.sub(lambda m: replacements[m.group(0)], _PAGE_TEMPLATE)


class SetupError(Exception):
    """Erro de request na tela de setup (token ausente/errado, corpo inválido)."""


def require_token(headers):
    # `compare_digest` em vez de `!=`: comparação de string em Python sai no primeiro
    # byte diferente, e as rotas de escrita ficam expostas a qualquer página que o
    # navegador do usuário abra.
    # Em bytes: cabeçalho HTTP é decodificado em latin-1, e `compare_digest` levanta
    # TypeError com `str` fora de ASCII — um byte alto no header viraria 500 em vez do
    # 400 claro.
    supplied = (headers.get("X-Setup-Token") or "").encode("utf-8", "replace")
    if not secrets.compare_digest(supplied, SETUP_TOKEN.encode("utf-8")):
        raise SetupError(f"Setup page session token missing or invalid; open {setup_origin()}/setup again.")


def handle_models_get():
    model_defaults.ensure_recommended_models(True)
    cfg = config.load_config()
    chat = model_catalog.list_models("chat")
    return {
        "chat": chat,
        "chat_lightweight": [m for m in chat if model_catalog.is_lightweight(m)],
        "embedding": model_catalog.list_models("embedding"),
        "rerank": model_catalog.list_models("rerank"),
        "current": {
            "router_model": cfg.get("router_model", ""),
            "scope_model": cfg.get("scope_model", ""),
            "research_model": cfg.get("research_model", ""),
            "research_max_iterations": cfg.get("research_max_iterations", config.DEFAULT_CONFIG["research_max_iterations"]),
            "quick_search_timeout_s": cfg.get("quick_search_timeout_s", config.DEFAULT_CONFIG["quick_search_timeout_s"]),
            "site_memory_similarity_threshold": cfg.get("site_memory_similarity_threshold", config.DEFAULT_CONFIG["site_memory_similarity_threshold"]),
            "embedding_model": cfg.get("embedding_model", ""),
            "rerank_model": cfg.get("rerank_model", ""),
        },
        "recommended": {
            **{field: model_id for field, (_mode, model_id) in model_defaults.RECOMMENDED_MODELS.items()},
            "research_max_iterations": config.DEFAULT_CONFIG["research_max_iterations"],
            "quick_search_timeout_s": config.DEFAULT_CONFIG["quick_search_timeout_s"],
            "site_memory_similarity_threshold": config.DEFAULT_CONFIG["site_memory_similarity_threshold"],
        },
        "ranges": {
            "research_max_iterations": list(config.RESEARCH_MAX_ITERATIONS_RANGE),
            "quick_search_timeout_s": list(config.QUICK_SEARCH_TIMEOUT_RANGE),
            "site_memory_similarity_threshold": list(config.SITE_MEMORY_SIMILARITY_THRESHOLD_RANGE),
        },
    }


def handle_local_models_get():
    available = local_embedder.info() if local_embedder.configured() else None
    return {"mode": config.local_models_mode(), "modes": list(config.LOCAL_MODEL_MODES),
            "sidecar": {"configured": local_embedder.configured(), "url": local_embedder.url(),
                        "embedding_model": (available or {}).get("model")}}


def handle_local_models_save(body):
    if not isinstance(body, dict) or body.get("mode") not in config.LOCAL_MODEL_MODES:
        raise SetupError("Invalid mode: use fallback, off or prefer.")
    try:
        config.update_config(lambda cfg: cfg.update(local_models=body["mode"]))
    except config.ConfigCorruptedError as exc:
        raise SetupError(str(exc))
    except OSError as exc:
        raise SetupError(f"Disk error while saving the configuration ({exc}); try again.")
    return {"ok": True, **handle_local_models_get()}


def handle_integration_get():
    registered = endpoint_sync.registrations()
    clients = []
    for key, info in AGENT_CLIENTS.items():
        spec, state = client_hooks.CLIENTS[key], client_hooks.status(info)
        clients.append({"client": key, "label": spec["label"], "mcp_url": registered.get(key), "hook": state["installed"],
                        "hook_error": state["error"], "file": spec["files"][0], "docs": spec["docs"],
                        "register_command": " ".join(endpoint_sync.register_command(key, setup_origin()))})
    try:
        mode, mode_error = config.hook_mode(), ""
    except ValueError as exc:
        mode, mode_error = "", str(exc)
    return {"daemon_url": setup_origin(), "clients": clients, "hook_mode": mode, "hook_mode_error": mode_error,
            "hook_modes": list(config.HOOK_MODES)}


def handle_integration_save(body):
    """`hook_mode` sets redirect/advise/off for every client; `register_mcp` runs the client's CLI; hook in two steps:
    `preview` shows the exact change, `install` writes it (with backup)."""
    if isinstance(body, dict) and body.get("action") == "hook_mode":
        if body.get("mode") not in config.HOOK_MODES:
            raise SetupError("Invalid mode: use redirect, advise or off.")
        try:
            config.update_config(lambda cfg: cfg.update(hook_mode=body["mode"]))
        except (OSError, config.ConfigCorruptedError) as exc:
            raise SetupError(str(exc))
        return handle_integration_get()
    if not isinstance(body, dict) or body.get("client") not in AGENT_CLIENTS:
        raise SetupError("Invalid client: use claude or codex.")
    info = AGENT_CLIENTS[body["client"]]
    try:
        if body.get("action") == "register_mcp":
            return {"command": endpoint_sync.register(body["client"], setup_origin()), **handle_integration_get()}
        if body.get("action") == "preview":
            return client_hooks.preview(info)
        if body.get("action") == "install":
            return {**client_hooks.install(info, body.get("token"), True), **handle_integration_get()}
    except (ValueError, RuntimeError) as exc:
        raise SetupError(str(exc))
    raise SetupError("Invalid action: use hook_mode, register_mcp, preview or install.")


def handle_providers_get():
    return {"providers": {
        name: {**web_search_external.PROVIDERS[name], "status": config.provider_key_status(name)}
        for name in config.WEB_PROVIDER_NAMES
    }, "contact": config.load_config().get("contact") or "", "user_agent": version.user_agent(
        config.load_config().get("contact"))}


def handle_contact_save(body):
    """Each user's own contact for the User-Agent that public APIs (Wikimedia) ask for; never a shared default."""
    if not isinstance(body, dict) or not isinstance(body.get("contact", ""), str):
        raise SetupError("Send the contact as text.")
    try:
        contact = version.check_contact(body.get("contact"))
        config.update_config(lambda cfg: cfg.update(contact=contact))
    except (ValueError, OSError, config.ConfigCorruptedError) as exc:
        raise SetupError(str(exc))
    return handle_providers_get()


def handle_providers_save(body):
    if not isinstance(body, dict):
        raise SetupError("Send the provider and the action in a JSON object.")
    provider = body.get("provider")
    if provider not in config.WEB_PROVIDER_NAMES:
        raise SetupError("Unknown web provider.")
    action = body.get("action")
    if action == "save":
        key = body.get("api_key")
        if not isinstance(key, str) or not key.strip():
            raise SetupError("Paste the provider API key before saving.")
    elif action == "remove":
        key = ""
    else:
        raise SetupError("Invalid action: use save or remove.")
    validated = None
    if key:
        key = key.strip()
        label = web_search_external.PROVIDERS[provider]["label"]
        try:
            validated = web_search_external.validate_key(provider, key)
        except web_search_external.ProviderAuthError as exc:
            raise SetupError(f"{label} rejected the key ({exc}). Check that you copied the whole key from your account.")
        except ValueError as exc:
            raise SetupError(str(exc))
        except web_search_external.ProviderRateLimited:
            raise SetupError(f"{label} returned a rate limit while testing the key; try again in a few minutes.")
        except Exception as exc:
            raise SetupError(f"Could not test the key with {label}: {type(exc).__name__}: {exc}"[:300])
    try:
        config.set_provider_api_key(provider, key)
    except secret_store.SecretProtectionError as exc:
        raise SetupError(f"Could not protect the key in this Windows profile: {exc}")
    except (ValueError, config.ConfigCorruptedError, OSError) as exc:
        raise SetupError(str(exc))
    return {"ok": True, "provider": provider, "status": config.provider_key_status(provider),
            "validated_results": validated}


def handle_autostart_get():
    return autostart.status()


def handle_autostart_action(body):
    if not isinstance(body, dict):
        raise SetupError("Request body must be a JSON object with 'action'.")
    action = body.get("action")
    if action == "install":
        return autostart.install()
    if action == "remove":
        return autostart.remove()
    raise SetupError("Invalid action: use 'install' or 'remove'.")


_MODEL_FIELDS = {
    "router_model": "chat",
    "scope_model": "chat",
    "research_model": "chat",
    "embedding_model": "embedding",
    "rerank_model": "rerank",
}

# (cast, range, mensagem) — mesmos limites de config.py, validados aqui pra recusar com
# SetupError explicito em vez de deixar config.clamp_* engolir um valor ruim em silêncio.
_NUMERIC_FIELDS = {
    "research_max_iterations": (int, config.RESEARCH_MAX_ITERATIONS_RANGE, "research loop round limit"),
    "quick_search_timeout_s": (int, config.QUICK_SEARCH_TIMEOUT_RANGE, "quick search time limit"),
    "site_memory_similarity_threshold": (float, config.SITE_MEMORY_SIMILARITY_THRESHOLD_RANGE, "per-site memory similarity threshold"),
}


def handle_models_save(body):
    if not isinstance(body, dict):
        raise SetupError("Request body must be a JSON object with the models.")
    # Any id the gateway accepts (the catalog is only a suggestion); it is embedded in the status JSON of GET /setup.
    validated = {}
    for field, mode in _MODEL_FIELDS.items():
        if field not in body:
            continue
        value = str(body[field] or "").strip()
        if len(value) > 200 or any(ord(char) < 33 for char in value):
            raise SetupError(f"Invalid {mode} model id: {value[:60]!r} (no spaces, up to 200 characters).")
        validated[field] = value

    for field, (cast, (lo, hi), label) in _NUMERIC_FIELDS.items():
        if field not in body:
            continue
        try:
            value = cast(body[field])
        except (TypeError, ValueError):
            raise SetupError(f"'{label}' must be numeric, got: {body[field]!r}")
        if not (lo <= value <= hi):
            raise SetupError(f"'{label}' must be between {lo} and {hi}, got: {value}")
        validated[field] = value

    def _mutate(cfg):
        cfg.update(validated)

    try:
        config.update_config(_mutate)
    except config.ConfigCorruptedError as exc:
        raise SetupError(str(exc))
    except OSError as exc:
        raise SetupError(f"Disk error while saving the configuration ({exc}); try again.")
    return {"ok": True}


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Tool — Settings</title>
<style>
  :root {
    --bg: #e8ece7; --surface: #f6f8f4; --line: #cfd6cf; --ink: #1c2621; --muted: #5d6b63;
    --signal: #2450f0; --on-signal: #ffffff; --ok: #2d8a55; --bad: #c2381f; --warn: #a87400; --idle: #9aa69f;
    --display: "Bahnschrift", "DIN Alternate", "Segoe UI", sans-serif;
    --body: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif;
    --mono: "Cascadia Mono", "Cascadia Code", Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #121815; --surface: #1a221e; --line: #2c3731; --ink: #e3e9e4; --muted: #93a199;
      --signal: #7d98ff; --on-signal: #0c1433; --ok: #59c786; --bad: #ff7a61; --warn: #e2b24a; --idle: #4b5850; }
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); }
  a { color: var(--signal); }
  main { max-width: 1080px; margin: 0 auto; padding: 36px 28px 72px; }

  .masthead { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .masthead h1 { display: inline; font: 700 2.1rem/1 var(--display); font-stretch: 87.5%; letter-spacing: -.01em; margin: 0; }
  .masthead .where { font: .8rem var(--mono); color: var(--muted); margin-left: 12px; }
  .masthead nav a { font: 600 .9rem var(--display); text-decoration: none; letter-spacing: .02em; }
  .masthead nav a:hover { text-decoration: underline; }

  .path { list-style: none; margin: 28px 0 36px; padding: 26px 28px 22px; display: grid;
    grid-template-columns: repeat(4, 1fr); background: var(--surface); border: 1px solid var(--line); border-radius: 14px; }
  .station { position: relative; }
  .station a { display: block; color: inherit; text-decoration: none; padding-right: 18px; border-radius: 8px; }
  .station a:focus-visible { outline: 2px solid var(--signal); outline-offset: 6px; }
  .node { display: flex; align-items: center; height: 22px; }
  .dot { width: 18px; height: 18px; border-radius: 50%; border: 3px solid var(--idle); background: var(--surface); flex: none; position: relative; z-index: 1; transition: border-color .3s, background .3s; }
  .wire { flex: 1; height: 3px; margin-left: -1px; background: var(--line); position: relative; overflow: hidden; }
  .wire::after { content: ""; position: absolute; inset: 0; background: var(--signal); transform: scaleX(0); transform-origin: left; }
  .station:last-child .wire { display: none; }
  .station[data-state="ok"] .dot { border-color: var(--ok); background: var(--ok); }
  .station[data-state="bad"] .dot { border-color: var(--bad); }
  .station[data-state="warn"] .dot { border-color: var(--warn); }
  .station[data-live="1"] .wire::after { transform: scaleX(1); transition: transform .45s cubic-bezier(.6,0,.2,1) var(--delay, 0s); }
  .station[data-break="1"] .wire { background: repeating-linear-gradient(90deg, var(--bad) 0 6px, transparent 6px 11px); }
  .station h2 { font: 600 1.05rem/1.2 var(--display); letter-spacing: .03em; margin: 14px 0 3px; }
  .station .detail { font: .78rem/1.35 var(--mono); color: var(--muted); overflow-wrap: anywhere; }
  .station .verdict { font-size: .84rem; margin-top: 6px; }
  .station[data-state="bad"] .verdict { color: var(--bad); }
  .station[data-state="warn"] .verdict { color: var(--warn); }

  .panels { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); gap: 22px; align-items: start; }
  .col { display: grid; grid-template-columns: minmax(0, 1fr); gap: 22px; }
  section { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 22px 24px 24px; scroll-margin-top: 20px; }
  section > h2 { font: 600 1.2rem/1.2 var(--display); letter-spacing: .02em; margin: 0 0 4px; }
  section > .lede { color: var(--muted); font-size: .87rem; margin: 0 0 18px; }
  fieldset { border: 0; border-top: 1px solid var(--line); margin: 18px 0 0; padding: 16px 0 0; min-width: 0; }
  legend { font: 600 .78rem var(--display); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); padding: 0 10px 0 0; }
  fieldset .lede { color: var(--muted); font-size: .82rem; margin: 2px 0 12px; }
  .field { margin-bottom: 12px; }
  .pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
  label { display: block; font-size: .8rem; font-weight: 600; margin-bottom: 4px; }
  .hint { display: block; font-size: .76rem; color: var(--muted); margin-top: 3px; }
  select, input[type=text], input[type=url], input[type=number], input[type=password] {
    width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line);
    background: var(--bg); color: var(--ink); font: .84rem var(--mono); min-width: 0; max-width: 100%; }
  input[type=password] { font-family: var(--body); }
  input::placeholder { color: var(--idle); }
  select:focus-visible, input:focus-visible, button:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
  button { font: 600 .86rem var(--display); letter-spacing: .03em; border-radius: 8px; padding: 9px 16px; cursor: pointer;
    background: var(--signal); color: var(--on-signal); border: 1px solid var(--signal); }
  button.quiet { background: transparent; color: var(--ink); border-color: var(--line); }
  button:disabled { opacity: .45; cursor: default; }
  .state { display: inline-flex; align-items: center; gap: 8px; font-size: .86rem; }
  .state::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--idle); }
  .state[data-state="ok"]::before { background: var(--ok); }
  .state[data-state="bad"]::before { background: var(--bad); }
  .state[data-state="warn"]::before { background: var(--warn); }
  .mono { font-family: var(--mono); font-size: .8rem; overflow-wrap: anywhere; }
  .msg { font-size: .84rem; min-height: 1.3em; margin-top: 10px; }
  .msg.error { color: var(--bad); }
  .msg.ok { color: var(--ok); }
  .msg.warn { color: var(--warn); }
  .provider + .provider { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 18px; }
  .provider-head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 8px; }
  .provider-head strong { font: 600 .98rem var(--display); letter-spacing: .02em; }
  .blocked { color: var(--muted); font-size: .88rem; border: 1px dashed var(--line); border-radius: 10px; padding: 14px 16px; }

  body:not(.ready) .station[data-live="1"] .wire::after { transform: scaleX(0); }
  @media (max-width: 900px) { .panels { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 640px) {
    main { padding: 24px 16px 56px; }
    .path { grid-template-columns: minmax(0, 1fr); gap: 0; padding: 20px; }
    .station a { display: grid; grid-template-columns: 22px minmax(0, 1fr); column-gap: 14px; padding: 0 0 18px; }
    .node { flex-direction: column; height: auto; grid-row: span 3; }
    .wire { width: 3px; height: auto; flex: 1; margin: -1px 0 0; }
    .wire::after { transform: scaleY(0); transform-origin: top; }
    .station[data-live="1"] .wire::after { transform: scaleY(1); }
    body:not(.ready) .station[data-live="1"] .wire::after { transform: scaleY(0); }
    .station:last-child .wire { display: none; }
    .station h2 { margin-top: 0; }
    .pair { grid-template-columns: minmax(0, 1fr); }
  }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    .station[data-live="1"] .wire::after, .dot { transition: none; }
  }
</style>
</head>
<body>
<main>
  <header class="masthead">
    <div><h1>Smart Tool</h1><span class="where">__SETUP_ORIGIN__</span></div>
    <nav><a href="/projects">Projects and indexing →</a></nav>
  </header>

  <ol class="path" aria-label="Path of a search, from the agent to the models">
    <li class="station" id="st-agent" data-state="ok"><a href="#autostart">
      <div class="node"><span class="dot"></span><span class="wire"></span></div>
      <h2>Agent</h2><div class="detail">MCP at /mcp</div><div class="verdict">Claude Code, Codex and other MCP clients</div></a></li>
    <li class="station" id="st-daemon" data-state="ok"><a href="#autostart">
      <div class="node"><span class="dot"></span><span class="wire"></span></div>
      <h2>Smart Tool</h2><div class="detail">__SETUP_ORIGIN__</div><div class="verdict" id="st-daemon-verdict">Responding</div></a></li>
    <li class="station" id="st-gateway"><a href="#gateway">
      <div class="node"><span class="dot"></span><span class="wire"></span></div>
      <h2>Gateway</h2><div class="detail" id="st-gateway-detail">—</div><div class="verdict" id="st-gateway-verdict">Checking</div></a></li>
    <li class="station" id="st-models"><a href="#models">
      <div class="node"><span class="dot"></span><span class="wire"></span></div>
      <h2>Models</h2><div class="detail" id="st-models-detail">—</div><div class="verdict" id="st-models-verdict">Checking</div></a></li>
  </ol>

  <div class="panels">
    <div class="col">
      <section id="gateway">
        <h2>Model gateway</h2>
        <p class="lede">Where embedding, chat and, if available, rerank calls go. The OpenAI-compatible adapter covers OpenAI, OpenRouter, a LiteLLM proxy and Ollama; other adapters go in as a .py file in the folder shown below.</p>
        <div class="state" id="gateway-state" role="status">Checking</div>
        <div class="field" style="margin-top:16px">
          <label for="gateway-adapter">Adapter</label>
          <select id="gateway-adapter"></select>
          <span class="hint mono" id="gateway-dir"></span>
        </div>
        <div id="gateway-fields"></div>
        <div class="msg error" id="gateway-errors" role="status" hidden></div>
        <div class="actions">
          <button type="button" id="gateway-save">Test and save</button>
          <button type="button" class="quiet" id="gateway-clear">Disconnect</button>
        </div>
        <div class="msg" id="gateway-msg" role="status"></div>
      </section>

      <section id="models">
        <h2>Models</h2>
        <p class="lede">Any id the gateway accepts; the published catalog shows up as suggestions. Rerank is optional: without it the hybrid order applies.</p>
        <div class="blocked" id="models-blocked" hidden>Connect the gateway to list the catalog and choose models.</div>
        <div id="models-form" hidden>
          <fieldset>
            <legend>Code search</legend>
            <p class="lede">Indexing and smart_search.</p>
            <div class="pair">
              <div class="field"><label for="sel-embedding">Embedding</label><input id="sel-embedding" list="dl-embedding" autocomplete="off" spellcheck="false"><datalist id="dl-embedding"></datalist></div>
              <div class="field"><label for="sel-rerank">Rerank</label><input id="sel-rerank" list="dl-rerank" autocomplete="off" spellcheck="false"><datalist id="dl-rerank"></datalist></div>
            </div>
            <div class="field"><label for="sel-scope">Index classification</label><input id="sel-scope" list="dl-scope" autocomplete="off" spellcheck="false"><datalist id="dl-scope"></datalist>
              <span class="hint">Decides scope, code vs. documentation and languages during indexing. Runs once per project.</span></div>
          </fieldset>
          <fieldset>
            <legend>Router</legend>
            <div class="field"><label for="sel-router">Router model</label><input id="sel-router" list="dl-router" autocomplete="off" spellcheck="false"><datalist id="dl-router"></datalist>
              <span class="hint">Decides before each agent Grep/Read/Bash. Use a small model without reasoning: above 10 s routing is turned off.</span></div>
          </fieldset>
          <fieldset>
            <legend>Web research</legend>
            <div class="field"><label for="sel-research">Iterative research model</label><input id="sel-research" list="dl-research" autocomplete="off" spellcheck="false"><datalist id="dl-research"></datalist>
              <span class="hint">Used by web_search with depth=research. Empty uses the router model.</span></div>
            <div class="pair">
              <div class="field"><label for="num-research-iterations">Maximum rounds</label><input type="number" id="num-research-iterations" step="1"><span class="hint" id="research-iterations-hint"></span></div>
              <div class="field"><label for="num-quick-timeout">Quick search time limit (s)</label><input type="number" id="num-quick-timeout" step="1"><span class="hint" id="quick-timeout-hint"></span></div>
            </div>
            <div class="field"><label for="num-site-memory-threshold">Minimum per-site memory similarity</label><input type="number" id="num-site-memory-threshold" step="0.01"><span class="hint" id="site-memory-threshold-hint"></span></div>
          </fieldset>
          <div class="actions"><button type="button" id="btn-save-models">Save models</button></div>
          <div class="msg" id="models-msg" role="status"></div>
        </div>
      </section>
    </div>

    <div class="col">
      <section id="autostart">
        <h2>Start automatically</h2>
        <p class="lede">Starts Smart Tool when you sign in to Windows.</p>
        <div class="state" id="autostart-status" role="status">Checking</div>
        <div class="actions"><button type="button" class="quiet" id="btn-autostart-toggle" disabled>Checking</button></div>
        <div class="msg" id="autostart-msg" role="status"></div>
      </section>

      <section id="local-models-section">
        <h2>Local models</h2>
        <p class="lede">Embedding from the mcp-memory sidecar on this machine. Switching between gateway and local rebuilds the project indexes; going back to a model used before reuses the vector cache.</p>
        <div class="field"><label for="sel-local-models">Usage</label>
          <select id="sel-local-models">
            <option value="fallback">Fallback: only when the gateway lacks the model</option>
            <option value="prefer">Prefer local: even when the gateway has the model</option>
            <option value="off">Off: never use local</option>
          </select>
          <span class="hint" id="local-models-hint">Checking the sidecar</span></div>
        <div class="actions"><button type="button" id="btn-save-local-models">Save</button></div>
        <div class="msg" id="local-models-msg" role="status"></div>
      </section>

      <section id="agents-section">
        <h2>Agents</h2>
        <p class="lede">The Smart Tool MCP server and the hook that redirects Grep/Read/Bash to smart_search and WebSearch/WebFetch to web_search/web_fetch. In Claude Code the hook is a direct HTTP call to the daemon; in Codex, a thin command that forwards to the daemon. If the port changes, the daemon rewrites these URLs on its own.</p>
        <div class="field"><label for="sel-hook-mode">Hook behavior</label>
          <select id="sel-hook-mode">
            <option value="redirect">Redirect: block the native tool and point to Smart Tool (default)</option>
            <option value="advise">Advise only: let the native tool run and tell the agent what Smart Tool would do</option>
            <option value="off">Off: no routing</option>
          </select>
          <span class="hint" id="hook-mode-hint">Applies to every client on the next tool call; nothing is reinstalled.</span></div>
        <div class="actions"><button type="button" id="btn-save-hook-mode">Save behavior</button></div>
        <div id="agents-list"></div>
        <pre class="hint" id="agents-preview" hidden></pre>
        <div class="actions"><button type="button" id="btn-agents-confirm" hidden>Confirm and write</button></div>
        <div class="msg" id="agents-msg" role="status"></div>
      </section>

      <section id="providers-section">
        <h2>Web search providers</h2>
        <p class="lede">All of them work without a key, on each provider's public free tier. Add your own key only if you need more volume: it is tested with a real search before being saved and is protected by DPAPI on this Windows machine.</p>
        <div class="field"><label for="contact-input">Your contact for public APIs (optional)</label>
          <input id="contact-input" autocomplete="off" spellcheck="false" placeholder="you@example.com or https://your.site">
          <span class="hint" id="contact-hint">Wikimedia asks tools to identify a contact in the User-Agent. It stays on this machine and is sent only to the search APIs.</span></div>
        <div class="actions"><button type="button" id="btn-save-contact">Save contact</button></div>
        <div class="msg" id="contact-msg" role="status"></div>
        <div id="providers-list"></div>
      </section>
    </div>
  </div>
</main>
<script>
const TOKEN = __SETUP_TOKEN__;
let status = __STATUS_JSON__;
const $ = (id) => document.getElementById(id);

function setMsg(id, kind, text) { const el = $(id); el.className = "msg" + (kind ? " " + kind : ""); el.textContent = text; }

async function post(path, body) {
  const resp = await fetch(path, { method: "POST", headers: { "X-Setup-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.error) throw new Error(data.error || `HTTP error ${resp.status}.`);
  return data;
}

function station(id, state, live, broken) {
  const el = $(id);
  el.dataset.state = state;
  el.dataset.live = live ? "1" : "0";
  el.dataset.break = broken ? "1" : "0";
}

function renderPath() {
  const gw = status.gateway;
  const gwReady = gw.status === "ready";
  const modelsReady = status.models_status === "ready";
  const source = gw.label || "Not configured";
  $("st-gateway-detail").textContent = gw.label || "no adapter";
  $("st-gateway-verdict").textContent = gwReady ? source : (gw.error || "Unavailable");
  $("st-models-detail").textContent = [status.embedding_model, status.rerank_model].filter(Boolean).join(" · ") || "none chosen";
  $("st-models-verdict").textContent = status.models_message || (modelsReady ? "Ready" : "Choose the models");
  station("st-agent", "ok", true, false);
  station("st-daemon", "ok", gwReady, !gwReady);
  station("st-gateway", gwReady ? "ok" : "bad", gwReady && modelsReady, gwReady && !modelsReady);
  station("st-models", gwReady ? (modelsReady ? "ok" : "warn") : "idle", false, false);
  ["st-agent", "st-daemon", "st-gateway"].forEach((id, i) => $(id).style.setProperty("--delay", (i * 0.35) + "s"));
}

function renderGateway() {
  const gw = status.gateway;
  const ready = gw.status === "ready";
  const el = $("gateway-state");
  el.dataset.state = ready ? "ok" : "bad";
  el.textContent = ready ? "Connected through " + gw.label : (gw.error || "Gateway unavailable");
  $("gateway-dir").textContent = "Your adapters: " + gw.dir;
  $("gateway-errors").hidden = !gw.errors.length;
  $("gateway-errors").textContent = gw.errors.length ? "Adapters that failed to load: " + gw.errors.join(" · ") : "";
  const select = $("gateway-adapter");
  // Redraw only when the adapters or their stored state change: the 15 s refresh must not wipe what is being typed.
  const signature = JSON.stringify([gw.selected, gw.adapters]);
  if (signature !== gatewaySignature && document.activeElement !== select && !select.dataset.touched) {
    gatewaySignature = signature;
    select.replaceChildren(...gw.adapters.map((a) => new Option(a.label + (a.builtin ? "" : " (yours)"), a.name)));
    select.value = gw.selected || (gw.adapters[0] || {}).name || "";
    renderGatewayFields();
  }
  $("gateway-clear").disabled = !gw.selected;
  $("models-blocked").hidden = ready;
  $("models-form").hidden = !ready;
}

let gatewaySignature = null;
function renderGatewayFields() {
  const adapter = status.gateway.adapters.find((a) => a.name === $("gateway-adapter").value);
  const box = $("gateway-fields");
  box.replaceChildren();
  for (const field of (adapter || { fields: [] }).fields) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    wrap.innerHTML = `<label></label><input autocomplete="off" spellcheck="false"><span class="hint"></span>`;
    const input = wrap.querySelector("input");
    wrap.querySelector("label").textContent = field.label + (field.required ? "" : " (optional)");
    input.dataset.field = field.name;
    input.dataset.secret = field.secret ? "1" : "";
    if (field.secret) {
      input.type = "password";
      input.autocomplete = "new-password";
      input.placeholder = field.state === "configured" ? "Saved · paste another to replace" : "";
      const hint = field.state === "unreadable" ? "The saved value cannot be opened in this Windows profile; paste it again. " : "";
      wrap.querySelector(".hint").textContent = hint + (field.hint || "") + " Protected by DPAPI.";
      if (field.state === "configured") {
        const remove = document.createElement("button");
        remove.type = "button"; remove.className = "quiet"; remove.textContent = "Remove";
        remove.addEventListener("click", () => saveGateway({ adapter: adapter.name, remove_secret: field.name }));
        wrap.appendChild(remove);
      }
    } else {
      input.value = field.value || "";
      input.placeholder = field.placeholder || "";
      wrap.querySelector(".hint").textContent = field.hint || "";
    }
    box.appendChild(wrap);
  }
}
function gatewayValues() {
  const values = {};
  for (const input of $("gateway-fields").querySelectorAll("input")) {
    if (input.dataset.secret && !input.value.trim()) continue;
    values[input.dataset.field] = input.value.trim();
  }
  return values;
}
$("gateway-adapter").addEventListener("change", () => { $("gateway-adapter").dataset.touched = "1"; renderGatewayFields(); });

function render() { renderPath(); renderGateway(); }
render();
requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add("ready")));

async function saveGateway(body) {
  const buttons = [$("gateway-save"), $("gateway-clear")];
  buttons.forEach((b) => b.disabled = true);
  setMsg("gateway-msg", "", body.adapter ? "Testing…" : "Disconnecting…");
  try {
    const data = await post("/setup/gateway", body);
    status.gateway = data.gateway;
    delete $("gateway-adapter").dataset.touched;
    gatewaySignature = null;
    setMsg("gateway-msg", data.gateway.status === "ready" ? "ok" : "warn",
      body.adapter ? "Gateway saved and responding." : "Gateway disconnected.");
    await refreshStatus(true);
  } catch (error) {
    setMsg("gateway-msg", "error", error.message);
  } finally {
    buttons.forEach((b) => b.disabled = false);
    renderGateway();
  }
}
$("gateway-save").addEventListener("click", () => saveGateway({ adapter: $("gateway-adapter").value, values: gatewayValues() }));
$("gateway-clear").addEventListener("click", () => saveGateway({ adapter: "" }));

let autostartState = { installed: false, method: null };
function renderAutostart() {
  const el = $("autostart-status");
  el.dataset.state = autostartState.installed ? "ok" : "warn";
  el.textContent = autostartState.installed ? `On via ${autostartState.method === "task" ? "Scheduled Task" : "Startup folder"}` : "Off";
  $("st-daemon-verdict").textContent = autostartState.installed ? "Responding · starts with Windows" : "Responding · manual start";
  const btn = $("btn-autostart-toggle");
  btn.disabled = false;
  btn.textContent = autostartState.installed ? "Turn off automatic start" : "Turn on automatic start";
}
async function loadAutostart() {
  try {
    const resp = await fetch("/setup/autostart");
    autostartState = await resp.json();
    renderAutostart();
  } catch (error) {
    $("autostart-status").dataset.state = "bad";
    $("autostart-status").textContent = "Status unavailable";
  }
}
loadAutostart();
$("btn-autostart-toggle").addEventListener("click", async () => {
  const action = autostartState.installed ? "remove" : "install";
  $("btn-autostart-toggle").disabled = true;
  setMsg("autostart-msg", "", action === "install" ? "Turning on…" : "Turning off…");
  try {
    const data = await post("/setup/autostart", { action });
    setMsg("autostart-msg", "ok", action === "install"
      ? (data.method === "task" ? "Turned on via Scheduled Task." : `Turned on via the Startup folder; the Scheduled Task was refused: ${data.fallback_reason || "no reason given"}.`)
      : "Turned off.");
    await loadAutostart();
  } catch (error) {
    setMsg("autostart-msg", "error", error.message);
    $("btn-autostart-toggle").disabled = false;
  }
});

function fillSelect(input, options, current) {
  $(input.getAttribute("list")).replaceChildren(...options.map((opt) => new Option(opt, opt)));
  input.value = current || "";
}
function fillNumber(id, hintId, range, current, recommended, unit) {
  const el = $(id);
  [el.min, el.max] = range;
  el.value = current;
  $(hintId).textContent = `Recommended ${recommended}${unit} · from ${range[0]} to ${range[1]}${unit}`;
}
let modelsLoaded = false;
async function loadModels() {
  try {
    const resp = await fetch("/setup/models");
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "The catalog did not respond.");
    fillSelect($("sel-router"), data.chat_lightweight.length ? data.chat_lightweight : data.chat, data.current.router_model);
    fillSelect($("sel-scope"), data.chat, data.current.scope_model);
    fillSelect($("sel-research"), data.chat, data.current.research_model);
    fillSelect($("sel-embedding"), data.embedding, data.current.embedding_model);
    fillSelect($("sel-rerank"), data.rerank, data.current.rerank_model);
    fillNumber("num-research-iterations", "research-iterations-hint", data.ranges.research_max_iterations, data.current.research_max_iterations, data.recommended.research_max_iterations, "");
    fillNumber("num-quick-timeout", "quick-timeout-hint", data.ranges.quick_search_timeout_s, data.current.quick_search_timeout_s, data.recommended.quick_search_timeout_s, " s");
    fillNumber("num-site-memory-threshold", "site-memory-threshold-hint", data.ranges.site_memory_similarity_threshold, data.current.site_memory_similarity_threshold, data.recommended.site_memory_similarity_threshold, "");
    modelsLoaded = true;
    setMsg("models-msg", "", "");
  } catch (error) {
    setMsg("models-msg", "error", "Could not read the gateway catalog: " + error.message);
  }
}
if (status.gateway.status === "ready") loadModels();

$("btn-save-models").addEventListener("click", async () => {
  if (!modelsLoaded) { setMsg("models-msg", "error", "The catalog has not loaded yet; nothing was saved."); return; }
  setMsg("models-msg", "", "Saving…");
  try {
    await post("/setup/models", {
      router_model: $("sel-router").value, scope_model: $("sel-scope").value, research_model: $("sel-research").value,
      research_max_iterations: Number($("num-research-iterations").value), quick_search_timeout_s: Number($("num-quick-timeout").value),
      site_memory_similarity_threshold: Number($("num-site-memory-threshold").value),
      embedding_model: $("sel-embedding").value, rerank_model: $("sel-rerank").value,
    });
    setMsg("models-msg", "ok", "Models saved.");
    await refreshStatus(true);
  } catch (error) {
    setMsg("models-msg", "error", error.message);
  }
});

async function refreshStatus(force) {
  try {
    const resp = await fetch("/setup/status");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const next = await resp.json();
    const reload = next.gateway.status === "ready" && (force || status.gateway.status !== "ready");
    status = next;
    render();
    if (reload && !modelsLoaded) loadModels();
  } catch (error) {
    $("st-daemon-verdict").textContent = "No response from the daemon";
    station("st-daemon", "bad", false, true);
  }
}
setInterval(refreshStatus, 15000);

async function loadLocalModels() {
  try {
    const resp = await fetch("/setup/local-models");
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error);
    $("sel-local-models").value = data.mode;
    $("local-models-hint").textContent = !data.sidecar.configured ? "mcp-memory sidecar not configured on this machine."
      : data.sidecar.embedding_model ? "Sidecar responding: " + data.sidecar.embedding_model
      : "Sidecar configured (" + data.sidecar.url + ") but not responding right now.";
  } catch (error) {
    $("local-models-hint").textContent = "Status unavailable: " + error.message;
  }
}
loadLocalModels();
$("btn-save-local-models").addEventListener("click", async () => {
  setMsg("local-models-msg", "", "Saving…");
  try {
    await post("/setup/local-models", { mode: $("sel-local-models").value });
    setMsg("local-models-msg", "ok", "Mode saved; the indexes adjust on the next search or check.");
    await loadLocalModels();
  } catch (error) {
    setMsg("local-models-msg", "error", error.message);
  }
});

let agentsPlan = null;
async function loadAgents() {
  try {
    const resp = await fetch("/setup/integration");
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error);
    if (document.activeElement !== $("sel-hook-mode")) $("sel-hook-mode").value = data.hook_mode;
    if (data.hook_mode_error) setMsg("agents-msg", "error", data.hook_mode_error);
    const list = $("agents-list");
    list.textContent = "";
    for (const item of data.clients) {
      const row = document.createElement("div");
      row.className = "provider";
      const mcp = item.mcp_url ? "MCP registered: " + item.mcp_url : "smart-tool MCP not registered in this client";
      const hook = item.hook_error ? "Hook unreadable: " + item.hook_error : item.hook ? "Hook installed and up to date" : "Hook missing or outdated";
      row.innerHTML = `<div class="provider-head"><strong></strong><span class="state"></span></div><p class="hint mcp"></p><p class="hint mono cmd"></p><p class="hint file"></p>
        <div class="actions"><button type="button" class="register">Register MCP</button><button type="button" class="hook">Install or update hook</button></div>`;
      row.querySelector("strong").textContent = item.label;
      row.querySelector(".state").textContent = hook;
      row.querySelector(".mcp").textContent = mcp;
      row.querySelector(".file").textContent = item.file;
      row.querySelector(".cmd").textContent = item.mcp_url ? "" : "Or in a terminal: " + item.register_command;
      const register = row.querySelector(".register");
      register.hidden = Boolean(item.mcp_url);
      register.addEventListener("click", async () => {
        register.disabled = true;
        setMsg("agents-msg", "", "Registering the MCP in " + item.label + "…");
        try {
          const result = await post("/setup/integration", { client: item.client, action: "register_mcp" });
          setMsg("agents-msg", "ok", "MCP registered with: " + result.command + ". Reconnect open sessions (/mcp).");
          await loadAgents();
$("btn-save-hook-mode").addEventListener("click", async () => {
  try {
    await post("/setup/integration", { action: "hook_mode", mode: $("sel-hook-mode").value });
    setMsg("agents-msg", "ok", "Hook behavior saved; it applies on the next tool call.");
    await loadAgents();
  } catch (error) {
    setMsg("agents-msg", "error", error.message);
  }
});
        } catch (error) {
          setMsg("agents-msg", "error", error.message);
          register.disabled = false;
        }
      });
      const button = row.querySelector(".hook");
      button.hidden = Boolean(item.hook);
      button.addEventListener("click", async () => {
        setMsg("agents-msg", "", "Preparing the change…");
        try {
          agentsPlan = { client: item.client, ...(await post("/setup/integration", { client: item.client, action: "preview" })) };
          $("agents-preview").textContent = item.file + "\\n" + JSON.stringify(agentsPlan.change, null, 2);
          $("agents-preview").hidden = false;
          $("btn-agents-confirm").hidden = false;
          setMsg("agents-msg", "", "Review the entry above; the file is backed up before writing.");
        } catch (error) {
          setMsg("agents-msg", "error", error.message);
        }
      });
      list.appendChild(row);
    }
  } catch (error) {
    setMsg("agents-msg", "error", "Agent status unavailable: " + error.message);
  }
}
loadAgents();
$("btn-agents-confirm").addEventListener("click", async () => {
  if (!agentsPlan) return;
  try {
    const result = await post("/setup/integration", { client: agentsPlan.client, action: "install", token: agentsPlan.token });
    setMsg("agents-msg", "ok", "Hook written. Backup: " + (result.backup || "new file") + (result.after_install ? ". " + result.after_install : ""));
    agentsPlan = null;
    $("agents-preview").hidden = true;
    $("btn-agents-confirm").hidden = true;
    await loadAgents();
  } catch (error) {
    setMsg("agents-msg", "error", error.message);
  }
});

const providerLabels = { keyless: ["warn", "No key · free tier"], configured: ["ok", "Key configured"], unreadable: ["bad", "Key unreadable in this Windows profile"] };
function providerCard(name, info) {
  const card = document.createElement("div");
  card.className = "provider";
  card.innerHTML = `
    <div class="provider-head"><strong></strong><span class="state" aria-live="polite"></span></div>
    <p class="hint free"></p><p class="hint keyed"></p>
    <p class="hint"><a target="_blank" rel="noopener noreferrer">Create a key in your provider account</a></p>
    <label>API key</label>
    <input type="password" autocomplete="new-password">
    <div class="actions"><button type="button" data-action="save">Validate and save</button><button type="button" class="quiet" data-action="remove">Remove key</button></div>
    <div class="msg" role="status"></div>`;
  card.querySelector("strong").textContent = info.label;
  card.querySelector(".free").textContent = info.free;
  card.querySelector(".keyed").textContent = info.keyed;
  const link = card.querySelector("a");
  link.href = info.signup;
  const input = card.querySelector("input");
  input.id = name + "-key";
  card.querySelector("label").htmlFor = input.id;
  input.placeholder = info.key_prefix ? `Paste the key (starts with ${info.key_prefix})` : "Paste the key from your account";
  const [kind, label] = providerLabels[info.status] || ["bad", info.status];
  Object.assign(card.querySelector(".state"), { textContent: label }).dataset.state = kind;
  const msg = card.querySelector(".msg");
  msg.id = name + "-msg";
  card.querySelector('[data-action="remove"]').disabled = info.status === "keyless";
  for (const button of card.querySelectorAll("button")) {
    button.addEventListener("click", async () => {
      const action = button.dataset.action;
      if (action === "save" && !input.value.trim()) { setMsg(msg.id, "error", "Paste the API key before saving."); return; }
      button.disabled = true;
      setMsg(msg.id, "", action === "save" ? "Testing the key with a real search…" : "Removing…");
      try {
        const data = await post("/setup/providers", { provider: name, action, ...(action === "save" ? { api_key: input.value.trim() } : {}) });
        input.value = "";
        await loadProviders();
        setMsg(name + "-msg", "ok", action === "save" ? `Key accepted by the provider (${data.validated_results} result(s) in the test) and protected on this Windows machine.` : "Key removed; free tier active.");
      } catch (error) {
        setMsg(msg.id, "error", error.message);
        button.disabled = false;
      }
    });
  }
  return card;
}
$("btn-save-contact").addEventListener("click", async () => {
  try {
    const data = await post("/setup/contact", { contact: $("contact-input").value.trim() });
    $("contact-hint").textContent = "Sent as: " + data.user_agent;
    setMsg("contact-msg", "ok", "Contact saved.");
  } catch (error) {
    setMsg("contact-msg", "error", error.message);
  }
});

async function loadProviders() {
  const list = $("providers-list");
  try {
    const resp = await fetch("/setup/providers");
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error);
    list.replaceChildren(...Object.entries(data.providers).map(([name, info]) => providerCard(name, info)));
    if (document.activeElement !== $("contact-input")) $("contact-input").value = data.contact;
    $("contact-hint").textContent = "Sent as: " + data.user_agent;
  } catch (error) {
    list.replaceChildren(Object.assign(document.createElement("p"), { className: "msg error", textContent: "Provider status unavailable: " + error.message }));
  }
}
loadProviders();
</script>
</body>
</html>
"""
