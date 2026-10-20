#!/usr/bin/env python3
"""Tela de configuração do Smart Tool, servida pelo daemon em `GET /setup`.

Uma seção por vez, escolhida na barra lateral: Overview (checklist do que precisa de
atenção, problemas primeiro), Agents (MCP, hook — inclusive desatualizado, com o que a
atualização muda — e controle de navegador), Models (gateway, modelos, modelos locais),
Web search (provedores e chaves protegidas por DPAPI), Agent rules, Redirects (métricas e
padrões de bloqueio), Setup audit e Startup (autostart).

`SETUP_TOKEN` é gerado uma vez por processo (nunca persistido em disco) e embutido no
HTML de `GET /setup`; toda rota `POST /setup/...` que escreve estado exige
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
import block_patterns
import browser_control
import client_hooks
import config
import daemon_launcher
import endpoint_sync
import gateway
import model_catalog
import model_defaults
import local_embedder
import project_store
import secret_store
import setup_audit
import integration_metrics
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
        "version": version.VERSION,
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
    try:
        browser, browser_error = browser_control.status(), ""
    except ValueError as exc:
        browser, browser_error = {}, str(exc)
    clients = []
    for key, info in AGENT_CLIENTS.items():
        spec, state = client_hooks.CLIENTS[key], client_hooks.status(info)
        clients.append({"client": key, "label": spec["label"], "mcp_url": registered.get(key), "hook": state["installed"],
                        "hook_state": state["state"], "hook_changes": state["changes"], "hook_error": state["error"],
                        "after_install": spec.get("after_install"), "file": spec["files"][0], "docs": spec["docs"],
                        "register_command": " ".join(endpoint_sync.register_command(key, setup_origin())),
                        "browser_control": browser.get(key), "browser_control_error": browser_error})
    try:
        mode, mode_error = config.hook_mode(), ""
    except ValueError as exc:
        mode, mode_error = "", str(exc)
    try:
        doc_mode, doc_error = config.doc_mode(), ""
    except ValueError as exc:
        doc_mode, doc_error = "", str(exc)
    try:
        duplicate_mode, duplicate_error = config.duplicate_mode(), ""
    except ValueError as exc:
        duplicate_mode, duplicate_error = "", str(exc)
    try:
        proposals_mode, proposals_error = config.pattern_proposals(), ""
    except ValueError as exc:
        proposals_mode, proposals_error = "", str(exc)
    try:
        stored, patterns_error = block_patterns.load(), ""
    except ValueError as exc:
        stored, patterns_error = block_patterns._empty(), str(exc)
    return {"daemon_url": setup_origin(), "clients": clients, "hook_mode": mode, "hook_mode_error": mode_error,
            "hook_modes": list(config.HOOK_MODES), "doc_mode": doc_mode, "doc_mode_error": doc_error,
            "doc_modes": list(config.DOC_MODES), "duplicate_mode": duplicate_mode,
            "duplicate_mode_error": duplicate_error, "duplicate_modes": list(config.DUPLICATE_MODES),
            "pattern_proposals": proposals_mode, "pattern_proposals_error": proposals_error,
            "block_patterns": {"proposals": stored["proposals"], "accepted": stored["accepted"],
                               "rejected": len(stored["rejected"]), "error": patterns_error},
            "redirects": integration_metrics.redirects()}


def handle_integration_save(body):
    """`hook_mode` sets redirect/advise/off for every client; `register_mcp` runs the client's CLI; hook in two steps:
    `preview` shows the exact change, `install` writes it (with backup)."""
    if isinstance(body, dict) and body.get("action") == "block_patterns":
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise SetupError("ids must be a list of pattern ids.")
        try:
            block_patterns.decide_proposals(ids, body.get("decision"))
        except (OSError, ValueError) as exc:
            raise SetupError(str(exc))
        return handle_integration_get()
    if isinstance(body, dict) and body.get("action") in ("hook_mode", "doc_mode", "duplicate_mode", "pattern_proposals"):
        action = body["action"]
        allowed = {"hook_mode": config.HOOK_MODES, "doc_mode": config.DOC_MODES,
                   "duplicate_mode": config.DUPLICATE_MODES, "pattern_proposals": config.PATTERN_PROPOSAL_MODES}[action]
        if body.get("mode") not in allowed:
            raise SetupError(f"Invalid mode: use {', '.join(allowed)}.")
        try:
            config.update_config(lambda cfg: cfg.update({action: body["mode"]}))
        except (OSError, config.ConfigCorruptedError) as exc:
            raise SetupError(str(exc))
        return handle_integration_get()
    if not isinstance(body, dict) or body.get("client") not in AGENT_CLIENTS:
        raise SetupError("Invalid client: use claude or codex.")
    info = AGENT_CLIENTS[body["client"]]
    try:
        if body.get("action") == "register_mcp":
            command = endpoint_sync.register(body["client"], setup_origin())
            return {"command": command, "browser_control": browser_control.ensure(body["client"]),
                    **handle_integration_get()}
        if body.get("action") == "browser_control":
            return {"browser_control": browser_control.ensure(body["client"]), **handle_integration_get()}
        if body.get("action") == "preview":
            return client_hooks.preview(info)
        if body.get("action") == "install":
            return {**client_hooks.install(info, body.get("token"), True), **handle_integration_get()}
    except (ValueError, RuntimeError) as exc:
        raise SetupError(str(exc))
    raise SetupError("Invalid action: use hook_mode, register_mcp, browser_control, preview or install.")


def handle_audit(body):
    """Setup audit panel: `index` lists the registered projects with their last audit summary; `report` returns the
    last stored report of one of them; `run` audits it now (MCP servers contacted unless probe is false). Only
    registered project folders are accepted. The opt-in session-start report in Claude Code: `hook_state`,
    `hook_preview` (the exact entry and a token), `hook_install` (with that token, after a backup) and `hook_remove`
    (only Smart Tool's entry, after a backup)."""
    if not isinstance(body, dict):
        raise SetupError("Invalid request.")
    roots = sorted({p["root"] for p in project_store.all_projects() if p.get("root")}, key=str.casefold)
    try:
        if body.get("action") == "hook_state":
            return {"state": client_hooks.audit_state(), "file": client_hooks.CLIENTS["claude"]["files"][0]}
        if body.get("action") == "hook_preview":
            return client_hooks.audit_preview()
        if body.get("action") == "hook_install":
            return client_hooks.audit_install(body.get("token"), True)
        if body.get("action") == "hook_remove":
            return client_hooks.audit_remove()
        if body.get("action") == "index":
            stored = setup_audit.index()
            found = {root: stored[setup_audit.index_key(root)] for root in roots if setup_audit.index_key(root) in stored}
            return {"projects": roots, "index": found, "schedule_s": setup_audit.SCHEDULE_S}
        root = body.get("project_root")
        if root not in roots:
            raise SetupError("Choose one of the registered projects.")
        if body.get("action") == "report":
            return {"report": setup_audit.last_report(root)}
        if body.get("action") == "run":
            return {"report": setup_audit.run(root, probe=body.get("probe") is not False)}
    except (OSError, ValueError) as exc:
        raise SetupError(str(exc))
    raise SetupError("Invalid action: use index, report or run.")


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
<title>Settings · Smart Tool</title>
<style>
  :root { color-scheme: light dark;
    --bg: #f6f7f9; --surface: #ffffff; --line: #dde1e6; --ink: #1b2430; --muted: #5b6472; --soft: #eef1f5;
    --accent: #2f6fed; --on-accent: #ffffff; --selected: #eaf1ff;
    --ok: #18723c; --ok-bg: #e6f4ea; --warn: #805300; --warn-bg: #fdf3dc; --bad: #b52c2c; --bad-bg: #fbe9e9; --idle: #5b6472; --idle-bg: #eef1f5;
    --body: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif; --mono: "Cascadia Mono", Consolas, monospace; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14171c; --surface: #1c2027; --line: #39414e; --ink: #e6e9ee; --muted: #a6afbb; --soft: #232831;
      --accent: #91baff; --on-accent: #14171c; --selected: #263751;
      --ok: #68dc93; --ok-bg: #163322; --warn: #e0b565; --warn-bg: #3a2e14; --bad: #ff9696; --bad-bg: #3d1d1d; --idle: #a6afbb; --idle-bg: #262b33; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 var(--body); }
  a { color: var(--accent); }
  [hidden] { display: none !important; }
  :focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }

  .top { max-width: 1240px; margin: 0 auto; padding: 26px 28px 18px; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .top h1 { margin: 0; font-size: 24px; letter-spacing: -.01em; }
  .top .where { color: var(--muted); font: 12px var(--mono); }
  .shell { max-width: 1240px; margin: 0 auto 60px; padding: 0 28px; display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 28px; align-items: start; }

  .side { position: sticky; top: 16px; display: flex; flex-direction: column; gap: 2px; }
  .side .group { margin: 16px 10px 6px; font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
  .side .group:first-child { margin-top: 0; }
  .side a { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 9px 12px; border-radius: 8px; color: var(--ink); text-decoration: none; border: 1px solid transparent; }
  .side a:hover { background: var(--soft); }
  .side a[aria-current="page"] { background: var(--selected); border-color: var(--accent); font-weight: 600; }
  .side .count { min-width: 22px; padding: 0 7px; border-radius: 11px; font-size: 12px; font-weight: 700; text-align: center; background: var(--warn-bg); color: var(--warn); }
  .side .count[data-tone="bad"] { background: var(--bad-bg); color: var(--bad); }
  .side .rule { margin: 18px 10px 0; font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); padding-top: 12px; }

  .page-head { margin: 0 0 18px; }
  .page-head h2 { margin: 0 0 4px; font-size: 26px; letter-spacing: -.01em; }
  .page-head p { margin: 0; color: var(--muted); max-width: 70ch; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; margin-bottom: 18px; }
  .card > h3 { margin: 0 0 4px; font-size: 18px; }
  .card > .lede { margin: 0 0 14px; color: var(--muted); font-size: 14px; max-width: 75ch; }
  .card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
  .card-head h3 { margin: 0; font-size: 18px; }

  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px 2px 4px; border-radius: 999px; font-size: 13px; font-weight: 600; white-space: nowrap; --tone: var(--idle); background: var(--idle-bg); color: var(--tone); }
  .pill::before { content: "–"; display: inline-grid; place-items: center; width: 18px; height: 18px; border-radius: 50%; font-size: 12px; font-weight: 800; background: var(--tone); color: var(--surface); }
  .pill[data-tone="ok"] { --tone: var(--ok); background: var(--ok-bg); }
  .pill[data-tone="ok"]::before { content: "✓"; }
  .pill[data-tone="warn"] { --tone: var(--warn); background: var(--warn-bg); }
  .pill[data-tone="warn"]::before { content: "!"; }
  .pill[data-tone="bad"] { --tone: var(--bad); background: var(--bad-bg); }
  .pill[data-tone="bad"]::before { content: "✕"; }
  .pill[data-tone="busy"]::before { content: "…"; }

  .summary-banner { display: flex; gap: 14px; align-items: center; padding: 16px 20px; border-radius: 12px; margin-bottom: 18px; background: var(--ok-bg); color: var(--ok); font-weight: 600; }
  .summary-banner[data-tone="warn"] { background: var(--warn-bg); color: var(--warn); }
  .summary-banner[data-tone="bad"] { background: var(--bad-bg); color: var(--bad); }
  .summary-banner[data-tone="busy"] { background: var(--idle-bg); color: var(--idle); }
  .checklist { list-style: none; margin: 0; padding: 0; }
  .check { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 16px; align-items: center; padding: 14px 0; border-top: 1px solid var(--line); }
  .check:first-child { border-top: 0; padding-top: 0; }
  .check-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-weight: 600; }
  .check-text { grid-column: 1; color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }
  .check-action { grid-column: 2; grid-row: 1 / span 2; }

  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 16px; padding: 16px 0; border-top: 1px solid var(--line); align-items: start; }
  .row:first-of-type { border-top: 0; }
  .row-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-weight: 600; }
  .row-text { grid-column: 1; color: var(--muted); font-size: 14px; max-width: 72ch; overflow-wrap: anywhere; }
  .row-side { grid-column: 2; grid-row: 1 / span 2; display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
  .row-more { grid-column: 1 / -1; }
  .callout { margin: 10px 0 0; padding: 12px 14px; border-radius: 8px; background: var(--warn-bg); color: var(--ink); font-size: 14px; }
  .callout strong { color: var(--warn); }
  .callout ul { margin: 6px 0 0; padding-left: 20px; }
  .callout.info { background: var(--soft); }
  .callout.info strong { color: var(--ink); }

  fieldset.choice { border: 0; padding: 16px 0; margin: 0; border-top: 1px solid var(--line); min-width: 0; }
  fieldset.choice:first-of-type { border-top: 0; padding-top: 4px; }
  fieldset.choice legend { font-weight: 600; font-size: 16px; padding: 0; float: left; width: 100%; }
  fieldset.choice legend + * { clear: both; }
  .choice-intro { color: var(--muted); font-size: 14px; margin: 4px 0 10px; max-width: 75ch; }
  .option { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 2px 10px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; margin-top: 8px; cursor: pointer; }
  .option:hover { border-color: var(--accent); }
  .option:has(input:checked) { border-color: var(--accent); background: var(--selected); }
  .option input { margin: 3px 0 0; width: 18px; height: 18px; accent-color: var(--accent); }
  .option-title { font-weight: 600; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .option-text { grid-column: 2; color: var(--muted); font-size: 14px; }
  .tag { font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--accent); border: 1px solid currentColor; border-radius: 4px; padding: 0 5px; }
  .saved { font-size: 13px; margin-top: 8px; min-height: 1.3em; }

  .field { margin: 0 0 14px; }
  .field label { display: block; font-weight: 600; margin: 0 0 3px; }
  .field .explain { display: block; color: var(--muted); font-size: 13px; margin: 0 0 6px; max-width: 72ch; }
  .field .hint { display: block; color: var(--muted); font-size: 12px; margin-top: 4px; }
  .pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0 16px; }
  input:not([type]), input[type=text], input[type=number], input[type=password], select {
    width: 100%; max-width: 560px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--ink); font: 14px var(--mono); }
  input[type=password] { font-family: var(--body); }
  button { font: 600 14px var(--body); min-height: 38px; padding: 7px 15px; border-radius: 6px; cursor: pointer; border: 1px solid var(--line); background: var(--surface); color: var(--ink); }
  button:hover { border-color: var(--accent); background: var(--selected); }
  button.primary { background: var(--accent); border-color: transparent; color: var(--on-accent); }
  button.primary:hover { filter: brightness(1.08); }
  button.small { min-height: 30px; padding: 3px 10px; font-size: 13px; }
  button:disabled { opacity: .5; cursor: default; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
  .msg { font-size: 14px; margin-top: 10px; min-height: 1.3em; overflow-wrap: anywhere; }
  .msg:empty { min-height: 0; margin: 0; }
  .msg.error { color: var(--bad); }
  .msg.ok { color: var(--ok); }
  .msg.warn { color: var(--warn); }
  .mono { font: 13px var(--mono); overflow-wrap: anywhere; }
  details.more { margin-top: 10px; }
  details.more > summary { cursor: pointer; color: var(--accent); font-size: 14px; }
  pre.code { margin: 8px 0 0; padding: 12px 14px; max-height: 320px; overflow: auto; font: 12px/1.5 var(--mono); background: var(--soft); border-radius: 8px; white-space: pre; }
  .confirm-box { margin-top: 12px; padding: 14px 16px; border: 1px solid var(--accent); border-radius: 8px; background: var(--selected); }
  .confirm-box p { margin: 0 0 6px; font-size: 14px; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 6px 0 16px; }
  .tile { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
  .tile .value { font-size: 28px; font-weight: 700; line-height: 1.1; }
  .tile .label { color: var(--muted); font-size: 13px; }
  .bars { list-style: none; margin: 6px 0 0; padding: 0; }
  .bars li { display: grid; grid-template-columns: minmax(90px, 200px) minmax(0, 1fr) 54px; gap: 10px; align-items: center; padding: 4px 0; font-size: 14px; }
  .bars .bar { height: 10px; border-radius: 5px; background: var(--soft); overflow: hidden; }
  .bars .bar span { display: block; height: 100%; background: var(--accent); border-radius: 5px; }
  .bars .num { text-align: right; font-variant-numeric: tabular-nums; }
  .bars .name { overflow-wrap: anywhere; }

  .provider-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 16px; padding: 16px 0; border-top: 1px solid var(--line); align-items: start; }
  .provider-row:first-of-type { border-top: 0; padding-top: 4px; }
  .provider-row .key-form { grid-column: 1 / -1; }

  .pattern-tools { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 10px; margin-top: 12px; }
  .pattern-list { list-style: none; margin: 10px 0 0; padding: 0; border-top: 1px solid var(--line); }
  .pattern-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; align-items: start; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .pattern-row.plain { grid-template-columns: minmax(0, 1fr); }
  .pattern-row input[type=checkbox] { margin-top: 6px; width: 18px; height: 18px; accent-color: var(--accent); }
  .pattern-body summary { display: flex; gap: 8px; align-items: baseline; cursor: pointer; min-width: 0; }
  .pattern-tool { font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); flex: none; }
  .pattern-code { font: 13px/1.4 var(--mono); overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .pattern-body[open] .pattern-code { -webkit-line-clamp: unset; }
  .pattern-detail { font-size: 14px; }
  .pattern-detail p { margin: 4px 0; }
  .pattern-meta { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 6px; flex-wrap: wrap; }
  .pattern-reach { font: 13px var(--mono); color: var(--muted); }
  .pager { justify-content: space-between; }

  .audit-list { list-style: none; padding: 0; margin: 10px 0; }
  .audit-row { padding: 10px 0; border-top: 1px solid var(--line); }
  .audit-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .audit-detail { font-size: 14px; margin: 4px 0 0; overflow-wrap: anywhere; color: var(--muted); }
  .new-tag { font-size: 12px; font-weight: 700; color: var(--accent); }
  dl.facts { margin: 0; }
  dl.facts > div { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 12px; padding: 10px 0; border-top: 1px solid var(--line); font-size: 14px; }
  dl.facts > div:first-child { border-top: 0; }
  dl.facts dt { color: var(--muted); }
  dl.facts dd { margin: 0; overflow-wrap: anywhere; }

  @media (max-width: 860px) {
    .shell { grid-template-columns: minmax(0, 1fr); gap: 16px; }
    .side { position: static; flex-direction: row; overflow-x: auto; gap: 6px; padding-bottom: 6px; }
    .side .group, .side .rule { display: none; }
    .side a { white-space: nowrap; }
    .pair, .pattern-tools { grid-template-columns: minmax(0, 1fr); }
    .row, .check, .provider-row { grid-template-columns: minmax(0, 1fr); }
    .row-side, .check-action { grid-column: 1; grid-row: auto; justify-content: flex-start; }
    .check-action { order: 1; justify-self: start; }
    dl.facts > div { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  }
</style>
</head>
<body>
<header class="top">
  <div><h1>Smart Tool settings</h1><span class="where">__SETUP_ORIGIN__ · version <span id="app-version"></span></span></div>
  <nav><a href="/projects">Projects and indexing →</a></nav>
</header>
<div class="shell">
  <nav class="side" aria-label="Settings sections">
    <a href="#overview" data-page="overview"><span>Overview</span><span class="count" data-count="overview" hidden></span></a>
    <div class="group">Connect</div>
    <a href="#agents" data-page="agents"><span>Agents</span><span class="count" data-count="agents" hidden></span></a>
    <a href="#models" data-page="models"><span>Models</span><span class="count" data-count="models" hidden></span></a>
    <a href="#web" data-page="web"><span>Web search</span><span class="count" data-count="web" hidden></span></a>
    <div class="group">Behavior</div>
    <a href="#rules" data-page="rules"><span>Agent rules</span><span class="count" data-count="rules" hidden></span></a>
    <a href="#redirects" data-page="redirects"><span>Redirects</span><span class="count" data-count="redirects" hidden></span></a>
    <div class="group">Maintenance</div>
    <a href="#audit" data-page="audit"><span>Setup audit</span><span class="count" data-count="audit" hidden></span></a>
    <a href="#startup" data-page="startup"><span>Startup</span><span class="count" data-count="startup" hidden></span></a>
    <p class="rule">Choices apply as soon as you pick them. Typed values (addresses, keys, model names) are saved with the button next to them.</p>
  </nav>

  <main>
    <section class="page" data-page="overview" aria-labelledby="h-overview">
      <header class="page-head"><h2 id="h-overview" tabindex="-1">Overview</h2>
        <p>Everything Smart Tool needs to work, checked on this machine. Anything that needs you comes first, with a button that takes you to the fix.</p></header>
      <div class="summary-banner" id="overview-banner" data-tone="busy" role="status">Checking…</div>
      <div class="card"><ul class="checklist" id="overview-list"></ul></div>
    </section>

    <section class="page" data-page="agents" aria-labelledby="h-agents" hidden>
      <header class="page-head"><h2 id="h-agents" tabindex="-1">Agents</h2>
        <p>Connect your coding agents to Smart Tool. Each agent needs two things: the connection (MCP server), which gives it smart_search, web_search and web_fetch, and the redirect hook, which sends its folder searches and web lookups to those tools.</p></header>
      <div class="msg" id="agents-msg" role="status"></div>
      <div id="agents-list"></div>
    </section>

    <section class="page" data-page="rules" aria-labelledby="h-rules" hidden>
      <header class="page-head"><h2 id="h-rules" tabindex="-1">Agent rules</h2>
        <p>How the hook treats your agents' tool calls. These apply to every connected agent on its next tool call; nothing needs reinstalling.</p></header>
      <div class="card" id="rules-card"></div>
    </section>

    <section class="page" data-page="redirects" aria-labelledby="h-redirects" hidden>
      <header class="page-head"><h2 id="h-redirects" tabindex="-1">Redirects</h2>
        <p>What the hook did with your agents' native searches, and what the agent did next. Below, the block patterns a model proposed from calls the fixed rule let through.</p></header>
      <div class="card" id="redirects-card"><p class="lede">Loading…</p></div>
      <div class="card">
        <h3>Block patterns</h3>
        <p class="lede">The hook decides with a fixed rule: content searches over a project folder go to smart_search. A model reviews the calls the rule let through, afterwards and without delaying them, and proposes patterns. Each proposal shows how many logged calls it would have blocked. Nothing blocks until you accept it.</p>
        <div class="msg" id="patterns-msg" role="status"></div>
        <div id="patterns-list"></div>
      </div>
    </section>

    <section class="page" data-page="models" aria-labelledby="h-models" hidden>
      <header class="page-head"><h2 id="h-models" tabindex="-1">Models</h2>
        <p>Smart Tool uses models for code search (embedding and rerank), for deciding where a search goes, and for web research. They come through a model gateway you connect here.</p></header>
      <div class="card" id="gateway">
        <div class="card-head"><h3>1. Model gateway</h3><span class="pill" id="gateway-pill" data-tone="busy">Checking</span></div>
        <p class="lede">Where embedding, chat and rerank calls go. The OpenAI-compatible adapter covers OpenAI, OpenRouter, a LiteLLM proxy and Ollama. Other adapters go in as a .py file in the folder shown below.</p>
        <div class="field">
          <label for="gateway-adapter">Adapter</label>
          <select id="gateway-adapter"></select>
          <span class="hint mono" id="gateway-dir"></span>
        </div>
        <div id="gateway-fields"></div>
        <div class="msg error" id="gateway-errors" role="status" hidden></div>
        <div class="actions">
          <button type="button" class="primary" id="gateway-save">Test and save</button>
          <button type="button" id="gateway-clear">Disconnect</button>
        </div>
        <div class="msg" id="gateway-msg" role="status"></div>
      </div>

      <div class="card" id="models-card">
        <div class="card-head"><h3>2. Models</h3><span class="pill" id="models-pill" data-tone="busy">Checking</span></div>
        <p class="lede">Any model id your gateway accepts. The gateway's catalog shows up as suggestions while you type.</p>
        <p class="callout info" id="models-blocked" hidden><strong>Connect the gateway first.</strong> The catalog and the model choice open once the gateway answers.</p>
        <div id="models-form" hidden>
          <div class="pair">
            <div class="field"><label for="sel-embedding">Embedding</label><span class="explain">Turns code into vectors for indexing and smart_search.</span><input id="sel-embedding" list="dl-embedding" autocomplete="off" spellcheck="false"><datalist id="dl-embedding"></datalist></div>
            <div class="field"><label for="sel-rerank">Rerank (optional)</label><span class="explain">Reorders search results by relevance. Empty keeps the hybrid order.</span><input id="sel-rerank" list="dl-rerank" autocomplete="off" spellcheck="false"><datalist id="dl-rerank"></datalist></div>
          </div>
          <div class="pair">
            <div class="field"><label for="sel-scope">Index classification</label><span class="explain">Decides scope, code or documentation, and languages while indexing. Runs once per project.</span><input id="sel-scope" list="dl-scope" autocomplete="off" spellcheck="false"><datalist id="dl-scope"></datalist></div>
            <div class="field"><label for="sel-router">Router</label><span class="explain">Decides before each agent Grep, Read or Bash. Use a small model without reasoning: above 10 s routing turns off.</span><input id="sel-router" list="dl-router" autocomplete="off" spellcheck="false"><datalist id="dl-router"></datalist></div>
          </div>
          <div class="field"><label for="sel-research">Web research</label><span class="explain">Used by web_search with depth=research. Empty uses the router model.</span><input id="sel-research" list="dl-research" autocomplete="off" spellcheck="false"><datalist id="dl-research"></datalist></div>
          <details class="more">
            <summary>Advanced web research limits</summary>
            <div class="pair" style="margin-top:12px">
              <div class="field"><label for="num-research-iterations">Maximum research rounds</label><span class="explain">How many search rounds a research may run before answering.</span><input type="number" id="num-research-iterations" step="1"><span class="hint" id="research-iterations-hint"></span></div>
              <div class="field"><label for="num-quick-timeout">Quick search time limit (s)</label><span class="explain">How long a quick web search waits for its sources.</span><input type="number" id="num-quick-timeout" step="1"><span class="hint" id="quick-timeout-hint"></span></div>
            </div>
            <div class="field"><label for="num-site-memory-threshold">Per-site memory similarity</label><span class="explain">How close a new question must be to a remembered page of the same site to reuse it.</span><input type="number" id="num-site-memory-threshold" step="0.01"><span class="hint" id="site-memory-threshold-hint"></span></div>
          </details>
          <div class="actions"><button type="button" class="primary" id="btn-save-models">Save models</button></div>
          <div class="msg" id="models-msg" role="status"></div>
        </div>
      </div>

      <div class="card" id="local-models-card">
        <div class="card-head"><h3>3. Local models</h3><span class="pill" id="local-pill" data-tone="busy">Checking</span></div>
        <p class="lede">Embedding from the mcp-memory sidecar on this machine. Switching between gateway and local rebuilds the project indexes; going back to a model used before reuses its vector cache.</p>
        <fieldset class="choice" id="local-models-choice"><legend>When to use the local model</legend></fieldset>
      </div>
    </section>

    <section class="page" data-page="web" aria-labelledby="h-web" hidden>
      <header class="page-head"><h2 id="h-web" tabindex="-1">Web search</h2>
        <p>web_search asks several providers at once. All of them work without a key on their public free tier; add your own key only if you need more volume.</p></header>
      <div class="card">
        <h3>Providers</h3>
        <p class="lede">A key is tested with a real search before it is saved, and is protected by DPAPI in this Windows profile.</p>
        <div id="provider-list"><p class="lede">Loading…</p></div>
      </div>
      <div class="card">
        <h3>Your contact (optional)</h3>
        <p class="lede">Wikimedia asks tools to name a contact in their User-Agent. It stays on this machine and is sent only to the search APIs.</p>
        <div class="field"><label for="contact-input">Email or website</label>
          <input id="contact-input" autocomplete="off" spellcheck="false" placeholder="you@example.com or https://your.site">
          <span class="hint mono" id="contact-hint"></span></div>
        <div class="actions"><button type="button" class="primary" id="btn-save-contact">Save contact</button></div>
        <div class="msg" id="contact-msg" role="status"></div>
      </div>
    </section>

    <section class="page" data-page="audit" aria-labelledby="h-audit" hidden>
      <header class="page-head"><h2 id="h-audit" tabindex="-1">Setup audit</h2>
        <p>What Claude Code and Codex load for a project, and where they collide: hooks deciding on the same tool at once, hooks or MCP servers that do not work, tools offered twice, skills and instructions one agent never sees. It runs on its own for the registered projects and never changes anything.</p></header>
      <div class="card">
        <div class="card-head"><h3>Report at session start (Claude Code)</h3><span class="pill" id="audit-hook-pill" data-tone="busy">Checking</span></div>
        <p class="lede">Off by default. When on, every new Claude Code session starts with one short line about the stored audit of its folder (errors and warnings only, nothing runs). It adds a SessionStart entry to <span class="mono" id="audit-hook-file"></span>, written after a backup, and turning it off removes only that entry. Without it, the agent gets the same line once, on its first search.</p>
        <div class="actions"><button type="button" class="primary" id="btn-audit-hook-on" hidden>Turn on</button><button type="button" id="btn-audit-hook-off" hidden>Turn off</button></div>
        <div id="audit-hook-confirm" hidden>
          <p class="hint" id="audit-hook-where"></p>
          <pre class="mono" id="audit-hook-preview"></pre>
          <div class="actions"><button type="button" class="primary" id="btn-audit-hook-write">Write it</button><button type="button" id="btn-audit-hook-cancel">Cancel</button></div>
        </div>
        <div class="msg" id="audit-hook-msg" role="status"></div>
      </div>
      <div class="card">
        <div class="field"><label for="sel-audit-project">Project</label>
          <select id="sel-audit-project"></select>
          <span class="hint" id="audit-hint"></span></div>
        <div class="actions"><button type="button" class="primary" id="btn-audit-run">Run now</button></div>
        <div class="msg" id="audit-msg" role="status"></div>
        <div id="audit-report"></div>
      </div>
    </section>

    <section class="page" data-page="startup" aria-labelledby="h-startup" hidden>
      <header class="page-head"><h2 id="h-startup" tabindex="-1">Startup</h2>
        <p>The Smart Tool service runs in the background on this machine. Your agents talk to it at the address below.</p></header>
      <div class="card">
        <div class="card-head"><h3>Start with Windows</h3><span class="pill" id="autostart-pill" data-tone="busy">Checking</span></div>
        <p class="lede">Starts Smart Tool when you sign in, so agents find it without you starting it by hand.</p>
        <div class="actions"><button type="button" id="btn-autostart-toggle" disabled>Checking</button></div>
        <div class="msg" id="autostart-msg" role="status"></div>
      </div>
      <div class="card">
        <h3>This installation</h3>
        <dl class="facts">
          <div><dt>Version</dt><dd id="fact-version"></dd></div>
          <div><dt>Service address</dt><dd class="mono">__SETUP_ORIGIN__</dd></div>
          <div><dt>MCP endpoint</dt><dd class="mono">__SETUP_ORIGIN__/mcp</dd></div>
        </dl>
      </div>
    </section>
  </main>
</div>
<script>
const TOKEN = __SETUP_TOKEN__;
let status = __STATUS_JSON__;
const $ = (id) => document.getElementById(id);
const loaded = { integration: null, autostart: null, providers: null, audit: null };
const failed = {};
let serviceDown = false;

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function button(label, className, onClick) {
  const el = node("button", className, label);
  el.type = "button";
  if (onClick) el.addEventListener("click", onClick);
  return el;
}
function pill(tone, label) {
  const el = node("span", "pill", label);
  el.dataset.tone = tone;
  return el;
}
function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
function setPill(id, tone, label) { const el = $(id); el.dataset.tone = tone; el.textContent = label; }
function setMsg(target, kind, text) {
  const el = typeof target === "string" ? $(target) : target;
  el.className = (el.dataset.base || "msg") + (kind ? " " + kind : "");
  el.textContent = text;
}
async function post(path, body) {
  const resp = await fetch(path, { method: "POST", headers: { "X-Setup-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.error) throw new Error(data.error || `HTTP error ${resp.status}.`);
  return data;
}
async function getJSON(path) {
  const resp = await fetch(path);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data.error) throw new Error(data.error || `HTTP error ${resp.status}.`);
  return data;
}

const PAGES = [...document.querySelectorAll("section.page")].map((section) => section.dataset.page);
function showPage(name, focus) {
  if (!PAGES.includes(name)) name = "overview";
  for (const section of document.querySelectorAll("section.page")) section.hidden = section.dataset.page !== name;
  for (const link of document.querySelectorAll(".side a")) {
    if (link.dataset.page === name) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  if (focus) $("h-" + name).focus();
}
function go(page) {
  if (location.hash === "#" + page) showPage(page, true); else location.hash = page;
}
window.addEventListener("hashchange", () => showPage(location.hash.slice(1), true));
showPage(location.hash.slice(1), false);
$("app-version").textContent = status.version;
$("fact-version").textContent = status.version;

function agentState(item) {
  if (!item.mcp_url) return { tone: "bad", label: "Not connected", action: "Connect",
    text: "The smart-tool MCP server is not registered in " + item.label + ", so it has no smart_search, web_search or web_fetch." };
  if (item.hook_error) return { tone: "bad", label: "Hook unreadable", action: "Open",
    text: "Could not read " + item.file + ": " + item.hook_error };
  if (item.hook_state === "outdated") return { tone: "warn", label: "Hook outdated", action: "Update hook", hook: true,
    text: "The redirect hook was written by an older Smart Tool. " + item.hook_changes.join(" ") };
  if (item.hook_state === "missing") return { tone: "warn", label: "No hook", action: "Install hook", hook: true,
    text: "Connected, but its folder searches and web lookups are not redirected to Smart Tool." };
  return { tone: "ok", label: "Connected", action: "Open", text: "Connection and redirect hook are up to date." };
}

function overviewItems() {
  const items = [];
  const gw = status.gateway;
  items.push(serviceDown
    ? { tone: "bad", label: "Not responding", title: "Smart Tool service", page: "startup", action: "Open", text: "The service stopped answering this page. Start Smart Tool again and reload." }
    : { tone: "ok", label: "Running", title: "Smart Tool service", page: "startup", action: "Open",
        text: "Answering at " + location.origin + "." + (loaded.autostart ? (loaded.autostart.installed ? " Starts with Windows." : " Not started with Windows.") : "") });
  items.push(gw.status === "ready"
    ? { tone: "ok", label: "Connected", title: "Model gateway", page: "models", action: "Open", text: "Model calls go through " + gw.label + "." }
    : { tone: "bad", label: "Not connected", title: "Model gateway", page: "models", action: "Connect", text: (gw.error || "No gateway configured.") + " Code search and routing need it." });
  if (gw.status === "ready") items.push(status.models_status === "ready"
    ? { tone: "ok", label: "Ready", title: "Models", page: "models", action: "Open", text: [status.embedding_model, status.rerank_model].filter(Boolean).join(" · ") || "Ready." }
    : { tone: "warn", label: "Choose models", title: "Models", page: "models", action: "Choose", text: status.models_message || "Some models are not chosen yet." });
  const integration = loaded.integration;
  if (failed.integration) items.push({ tone: "bad", label: "Unavailable", title: "Agents", page: "agents", action: "Open", text: failed.integration });
  else if (!integration) items.push({ tone: "busy", label: "Checking", title: "Agents", page: "agents", action: "Open", text: "Reading Claude Code and Codex settings…" });
  else {
    for (const item of integration.clients) {
      const state = agentState(item);
      items.push({ ...state, title: item.label, page: "agents", run: state.hook ? () => { go("agents"); hookControls[item.client](); } : null });
    }
    for (const rule of RULES) {
      if (integration[rule.key + "_error"]) items.push({ tone: "bad", label: "Unreadable", title: rule.title, page: "rules", action: "Open", text: integration[rule.key + "_error"] });
    }
    if (integration.hook_mode === "off") items.push({ tone: "warn", label: "Off", title: "Redirects", page: "rules", action: "Turn on", text: "The hook lets every native search run; agents are not sent to Smart Tool." });
    const waiting = integration.block_patterns.proposals.length;
    if (integration.block_patterns.error) items.push({ tone: "bad", label: "Unreadable", title: "Block patterns", page: "redirects", action: "Open", text: integration.block_patterns.error });
    else if (waiting) items.push({ tone: "warn", label: waiting + " waiting", title: "Block patterns", page: "redirects", action: "Review", text: "A model proposed " + plural(waiting, "pattern") + " to block. Nothing blocks until you accept." });
  }
  if (failed.providers) items.push({ tone: "bad", label: "Unavailable", title: "Web search", page: "web", action: "Open", text: failed.providers });
  else if (loaded.providers) {
    const list = Object.values(loaded.providers.providers);
    const unreadable = list.filter((info) => info.status === "unreadable");
    const keyed = list.filter((info) => info.status === "configured").length;
    items.push(unreadable.length
      ? { tone: "bad", label: "Key unreadable", title: "Web search", page: "web", action: "Fix", text: unreadable.map((info) => info.label).join(", ") + ": the saved key cannot be opened in this Windows profile. Paste it again." }
      : { tone: "ok", label: "Ready", title: "Web search", page: "web", action: "Open", text: plural(list.length, "provider") + " on their free tier" + (keyed ? ", " + keyed + " with your key." : "; no key needed.") });
  }
  if (failed.audit) items.push({ tone: "bad", label: "Unavailable", title: "Setup audit", page: "audit", action: "Open", text: failed.audit });
  else if (loaded.audit && loaded.audit.projects.length) {
    const errors = Object.values(loaded.audit.index).reduce((sum, entry) => sum + entry.summary.error, 0);
    items.push(errors
      ? { tone: "warn", label: plural(errors, "problem"), title: "Setup audit", page: "audit", action: "See report", text: "Hooks or MCP servers that collide or do not work in your registered projects." }
      : { tone: "ok", label: "Clean", title: "Setup audit", page: "audit", action: "Open", text: "No collision or broken piece in the audited projects." });
  }
  const rank = { bad: 0, warn: 1, busy: 2, idle: 3, ok: 4 };
  return items.map((item, index) => ({ ...item, index })).sort((a, b) => rank[a.tone] - rank[b.tone] || a.index - b.index);
}

function renderOverview() {
  const items = overviewItems();
  const list = $("overview-list");
  list.replaceChildren();
  for (const item of items) {
    const row = node("li", "check");
    const title = node("div", "check-title");
    title.append(pill(item.tone, item.label), node("span", "", item.title));
    const action = button(item.action, item.tone === "bad" || item.tone === "warn" ? "primary" : "", item.run || (() => go(item.page)));
    action.setAttribute("aria-label", item.action + ": " + item.title);
    const side = node("div", "check-action");
    side.appendChild(action);
    row.append(title, side, node("div", "check-text", item.text));
    list.appendChild(row);
  }
  const problems = items.filter((item) => item.tone === "bad" || item.tone === "warn");
  const pending = items.some((item) => item.tone === "busy");
  const banner = $("overview-banner");
  banner.dataset.tone = problems.some((item) => item.tone === "bad") ? "bad" : problems.length ? "warn" : pending ? "busy" : "ok";
  banner.textContent = problems.length ? problems.length + (problems.length === 1 ? " thing needs" : " things need") + " your attention."
    : pending ? "Checking…" : "Everything is working.";
  const counts = {};
  for (const item of problems) {
    const entry = counts[item.page] || (counts[item.page] = { count: 0, bad: false });
    entry.count += 1;
    entry.bad = entry.bad || item.tone === "bad";
  }
  counts.overview = { count: problems.length, bad: problems.some((item) => item.tone === "bad") };
  for (const badge of document.querySelectorAll(".side .count")) {
    const entry = counts[badge.dataset.count];
    badge.hidden = !entry;
    badge.textContent = entry ? entry.count : "";
    badge.dataset.tone = entry && entry.bad ? "bad" : "warn";
    badge.title = entry ? plural(entry.count, "item") + (entry.count === 1 ? " needs" : " need") + " attention" : "";
  }
}

let gatewaySignature = null;
function renderGateway() {
  const gw = status.gateway;
  const ready = gw.status === "ready";
  setPill("gateway-pill", ready ? "ok" : "bad", ready ? "Connected through " + gw.label : "Not connected");
  if (!ready) setMsg("gateway-msg", "error", gw.error || "Gateway unavailable.");
  $("gateway-dir").textContent = "Your adapters: " + gw.dir;
  $("gateway-errors").hidden = !gw.errors.length;
  $("gateway-errors").textContent = gw.errors.length ? "Adapters that failed to load: " + gw.errors.join(" · ") : "";
  const select = $("gateway-adapter");
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
  if (!ready) setPill("models-pill", "idle", "Waiting for the gateway");
  else if (status.models_status === "ready") setPill("models-pill", "ok", "Ready");
  else setPill("models-pill", "warn", "Choose models");
}
function renderGatewayFields() {
  const adapter = status.gateway.adapters.find((a) => a.name === $("gateway-adapter").value);
  const box = $("gateway-fields");
  box.replaceChildren();
  for (const field of (adapter || { fields: [] }).fields) {
    const wrap = node("div", "field");
    const input = node("input");
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.id = "gateway-field-" + field.name;
    const label = node("label", "", field.label + (field.required ? "" : " (optional)"));
    label.htmlFor = input.id;
    const hint = node("span", "hint");
    input.dataset.field = field.name;
    input.dataset.secret = field.secret ? "1" : "";
    if (field.secret) {
      input.type = "password";
      input.autocomplete = "new-password";
      input.placeholder = field.state === "configured" ? "Saved · paste another to replace" : "";
      hint.textContent = (field.state === "unreadable" ? "The saved value cannot be opened in this Windows profile; paste it again. " : "")
        + (field.hint || "") + " Protected by DPAPI.";
    } else {
      input.value = field.value || "";
      input.placeholder = field.placeholder || "";
      hint.textContent = field.hint || "";
    }
    wrap.append(label, input, hint);
    if (field.secret && field.state === "configured") {
      wrap.appendChild(button("Remove saved value", "small", () => saveGateway({ adapter: adapter.name, remove_secret: field.name })));
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
async function saveGateway(body) {
  const buttons = [$("gateway-save"), $("gateway-clear")];
  buttons.forEach((b) => b.disabled = true);
  setMsg("gateway-msg", "", body.adapter ? "Testing the connection…" : "Disconnecting…");
  try {
    const data = await post("/setup/gateway", body);
    status.gateway = data.gateway;
    delete $("gateway-adapter").dataset.touched;
    gatewaySignature = null;
    setMsg("gateway-msg", data.gateway.status === "ready" ? "ok" : "warn",
      body.adapter ? (data.gateway.status === "ready" ? "Saved. The gateway answered." : "Saved, but the gateway does not answer: " + (data.gateway.error || "no reason given."))
        : "Gateway disconnected.");
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

function fillSuggestions(input, options, current) {
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
    const data = await getJSON("/setup/models");
    fillSuggestions($("sel-router"), data.chat_lightweight.length ? data.chat_lightweight : data.chat, data.current.router_model);
    fillSuggestions($("sel-scope"), data.chat, data.current.scope_model);
    fillSuggestions($("sel-research"), data.chat, data.current.research_model);
    fillSuggestions($("sel-embedding"), data.embedding, data.current.embedding_model);
    fillSuggestions($("sel-rerank"), data.rerank, data.current.rerank_model);
    fillNumber("num-research-iterations", "research-iterations-hint", data.ranges.research_max_iterations, data.current.research_max_iterations, data.recommended.research_max_iterations, "");
    fillNumber("num-quick-timeout", "quick-timeout-hint", data.ranges.quick_search_timeout_s, data.current.quick_search_timeout_s, data.recommended.quick_search_timeout_s, " s");
    fillNumber("num-site-memory-threshold", "site-memory-threshold-hint", data.ranges.site_memory_similarity_threshold, data.current.site_memory_similarity_threshold, data.recommended.site_memory_similarity_threshold, "");
    modelsLoaded = true;
    setMsg("models-msg", "", "");
  } catch (error) {
    setMsg("models-msg", "error", "Could not read the gateway catalog: " + error.message);
  }
}
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
    serviceDown = false;
    renderGateway();
    if (reload && !modelsLoaded) loadModels();
  } catch (error) {
    serviceDown = true;
  }
  renderOverview();
}

function choiceGroup(fieldset, name, options, current, onPick) {
  for (const [value, title, text, recommended] of options) {
    const option = node("label", "option");
    const radio = node("input");
    radio.type = "radio";
    radio.name = name;
    radio.value = value;
    radio.checked = value === current;
    radio.addEventListener("change", () => { if (radio.checked) onPick(value); });
    const head = node("span", "option-title", title);
    if (recommended) head.appendChild(node("span", "tag", "Recommended"));
    option.append(radio, head, node("span", "option-text", text));
    fieldset.appendChild(option);
  }
  const saved = node("div", "saved msg");
  saved.dataset.base = "saved msg";
  saved.setAttribute("role", "status");
  fieldset.appendChild(saved);
  return saved;
}
function checkRadio(name, value) {
  for (const radio of document.querySelectorAll(`input[name="${name}"]`)) radio.checked = radio.value === value;
}

const LOCAL_OPTIONS = [
  ["fallback", "Only as a fallback", "Use the local model only when the gateway does not have the embedding model.", true],
  ["prefer", "Prefer local", "Use the local model even when the gateway has it; your code vectors stay on this machine."],
  ["off", "Never", "Always use the gateway."],
];
let localMode = null;
const localSaved = choiceGroup($("local-models-choice"), "local_models", LOCAL_OPTIONS, null, async (mode) => {
  setMsg(localSaved, "", "Saving…");
  try {
    await post("/setup/local-models", { mode });
    localMode = mode;
    setMsg(localSaved, "ok", "Saved. The indexes adjust on the next search or check.");
  } catch (error) {
    checkRadio("local_models", localMode);
    setMsg(localSaved, "error", error.message);
  }
});
async function loadLocalModels() {
  try {
    const data = await getJSON("/setup/local-models");
    localMode = data.mode;
    checkRadio("local_models", data.mode);
    if (!data.sidecar.configured) setPill("local-pill", "idle", "No sidecar on this machine");
    else if (data.sidecar.embedding_model) setPill("local-pill", "ok", "Sidecar answering: " + data.sidecar.embedding_model);
    else setPill("local-pill", "warn", "Sidecar not answering (" + data.sidecar.url + ")");
  } catch (error) {
    setPill("local-pill", "bad", "Status unavailable");
    setMsg(localSaved, "error", error.message);
  }
}

const RULES = [
  { key: "hook_mode", title: "Redirect behavior", applies: "Applies on the next tool call.",
    intro: "What the hook does when an agent reaches for a native search or web tool that Smart Tool covers.",
    options: [["redirect", "Redirect", "Stop the native tool and tell the agent which Smart Tool call to make instead.", true],
              ["advise", "Advise only", "Let the native tool run and tell the agent what Smart Tool would have done."],
              ["off", "Off", "No routing; agents use their native tools as usual."]] },
  { key: "doc_mode", title: "Documentation on edit", applies: "Applies on the next edit.",
    intro: "Checks the public functions an edit touches. Private, nested and override functions are exempt, as in pydocstyle, eslint-plugin-jsdoc and Checkstyle. A documented function comes up again only when its signature changes.",
    options: [["remind", "Remind", "The edit runs; the agent is told which touched functions lack a docstring.", true],
              ["require", "Require", "An edit that leaves a touched function without a docstring is blocked until it adds one."],
              ["off", "Off", "No documentation checks."]] },
  { key: "duplicate_mode", title: "Duplicate functions on edit", applies: "Applies on the next edit.",
    intro: "Compares the functions an edit writes with the indexed code: identical bodies, and near-identical ones with only local names changed. It never blocks.",
    options: [["warn", "Warn", "The edit runs; the agent is told when a function copies one already in the project.", true],
              ["off", "Off", "No duplicate checks."]] },
  { key: "pattern_proposals", title: "Block patterns proposed by a model", applies: "Applies to the next review.",
    intro: "Afterwards, never delaying a call, the scope model (else the router model) reviews searches the fixed rule let through and may propose block patterns. Nothing blocks until you accept it on the Redirects page.",
    options: [["on", "On", "Review in the background and list proposals for you.", true],
              ["off", "Off", "No review and no proposals."]] },
];
function renderRules() {
  const card = $("rules-card");
  if (card.dataset.built) {
    for (const rule of RULES) checkRadio(rule.key, loaded.integration[rule.key]);
    return;
  }
  card.dataset.built = "1";
  card.replaceChildren();
  for (const rule of RULES) {
    const fieldset = node("fieldset", "choice");
    fieldset.appendChild(node("legend", "", rule.title));
    fieldset.appendChild(node("p", "choice-intro", rule.intro));
    const saved = choiceGroup(fieldset, rule.key, rule.options, loaded.integration[rule.key], async (mode) => {
      setMsg(saved, "", "Saving…");
      try {
        await post("/setup/integration", { action: rule.key, mode });
        loaded.integration[rule.key] = mode;
        loaded.integration[rule.key + "_error"] = "";
        setMsg(saved, "ok", "Saved. " + rule.applies);
        renderOverview();
      } catch (error) {
        checkRadio(rule.key, loaded.integration[rule.key]);
        setMsg(saved, "error", error.message);
      }
    });
    if (loaded.integration[rule.key + "_error"]) setMsg(saved, "error", loaded.integration[rule.key + "_error"]);
    card.appendChild(fieldset);
  }
}

function browserState(item) {
  if (item.browser_control_error) return ["bad", "Unreadable", "Browser control status unreadable: " + item.browser_control_error];
  const state = item.browser_control || {};
  if (state.state === "registered") return ["ok", "On", "Agents can open pages in an isolated, headless Chromium through the Playwright MCP server."];
  if (state.state === "kept") return ["ok", "Your own server", "Your own playwright MCP server is kept as it is."];
  if (state.state === "undenied") return ["bad", "Safety rule missing", "Browser control is registered without its safety rule: " + state.reason];
  if (state.state === "unavailable") return ["idle", "Unavailable", "Not available on this machine (" + state.reason + "). Pages are still read by web_fetch."];
  return ["idle", "Not set up", "Set up together with the connection."];
}
const hookControls = {};
function row(titleText, pillEl, text) {
  const el = node("div", "row");
  const title = node("div", "row-title");
  title.append(node("span", "", titleText), pillEl);
  const side = node("div", "row-side");
  const body = node("div", "row-text", text);
  el.append(title, side, body);
  return { el, side, body };
}
function agentCard(item) {
  const card = node("article", "card");
  const state = agentState(item);
  const head = node("div", "card-head");
  head.append(node("h3", "", item.label), pill(state.tone, state.label));
  const msg = node("div", "msg");
  msg.setAttribute("role", "status");

  const mcp = row("Connection (MCP server)", item.mcp_url ? pill("ok", "Registered") : pill("bad", "Not registered"),
    item.mcp_url ? "Registered at " + item.mcp_url + ". Gives the agent smart_search, web_search and web_fetch."
      : "Not registered. Without it the agent has no smart_search, web_search or web_fetch.");
  if (!item.mcp_url) {
    const register = button("Connect", "primary", async () => {
      register.disabled = true;
      setMsg(msg, "", "Registering the MCP server in " + item.label + "…");
      try {
        const result = await post("/setup/integration", { client: item.client, action: "register_mcp" });
        await loadIntegration();
        setMsg(agentMsg(item.client), "ok", "Registered with: " + result.command + ". " + browserState({ browser_control: result.browser_control })[2]
          + " Reconnect open " + item.label + " sessions (/mcp) to see the tools.");
      } catch (error) {
        setMsg(msg, "error", error.message);
        register.disabled = false;
      }
    });
    mcp.side.appendChild(register);
    const manual = node("details", "more row-more");
    manual.append(node("summary", "", "Or run it yourself in a terminal"), node("pre", "code", item.register_command));
    mcp.el.appendChild(manual);
  }

  const hookPill = item.hook_error ? pill("bad", "Unreadable") : item.hook_state === "current" ? pill("ok", "Up to date")
    : item.hook_state === "outdated" ? pill("warn", "Outdated") : pill("warn", "Not installed");
  const hook = row("Redirect hook", hookPill, item.hook_error ? "Could not read " + item.file + ": " + item.hook_error
    : "Runs before the agent's own search and web tools: folder searches go to smart_search, web lookups to web_search and web_fetch, so the agent reads less. It lives in " + item.file + ".");
  if (item.hook_state === "outdated") {
    const why = node("div", "callout");
    why.append(node("strong", "", "Why update: "), document.createTextNode("this entry was written by an older Smart Tool version."));
    const list = node("ul");
    for (const change of item.hook_changes) list.appendChild(node("li", "", change));
    why.append(list, node("div", "", "Updating rewrites only Smart Tool's entry. Your other hooks stay as they are, and the file is backed up first."));
    hook.body.appendChild(why);
  } else if (item.hook_state === "missing" && !item.hook_error) {
    hook.body.appendChild(node("div", "callout info", "Installing adds one entry to this file. Your other hooks stay as they are, and the file is backed up first."));
  }
  const docs = node("a", "", "How " + item.label + " hooks work");
  docs.href = item.docs;
  docs.target = "_blank";
  docs.rel = "noopener noreferrer";
  const docsLine = node("div", "hint");
  docsLine.style.marginTop = "6px";
  docsLine.appendChild(docs);
  hook.body.appendChild(docsLine);

  const confirm = node("div", "confirm-box row-more");
  confirm.hidden = true;
  const confirmFile = node("p", "mono");
  const preview = node("pre", "code");
  const exact = node("details", "more");
  exact.append(node("summary", "", "Show the exact entry that will be written"), preview);
  const confirmMsg = node("div", "msg");
  confirmMsg.setAttribute("role", "status");
  const write = button("Confirm and write", "primary");
  const cancel = button("Cancel", "");
  const confirmActions = node("div", "actions");
  confirmActions.append(write, cancel);
  confirm.append(node("p", "", "Review the change, then confirm. The preview is valid for 10 minutes."), confirmFile, exact, confirmMsg, confirmActions);
  if (item.after_install) confirm.appendChild(node("p", "callout info", "After writing: " + item.after_install));
  hook.el.appendChild(confirm);

  const hookLabel = item.hook_state === "outdated" ? "Update hook" : "Install hook";
  const start = button(hookLabel, "primary");
  start.hidden = item.hook_state === "current" || Boolean(item.hook_error) || item.hook_state === null;
  hook.side.appendChild(start);
  async function openPreview() {
    start.disabled = true;
    setMsg(msg, "", "Preparing the change…");
    try {
      const plan = await post("/setup/integration", { client: item.client, action: "preview" });
      setMsg(msg, "", "");
      if (plan.installed) { await loadIntegration(); return; }
      confirmFile.textContent = (item.hook_state === "outdated" ? "Replaces Smart Tool's entries with these, by event, in " : "Adds these entries, by event, in ") + plan.file;
      preview.textContent = JSON.stringify(plan.change, null, 2);
      confirm.hidden = false;
      start.hidden = true;
      setMsg(confirmMsg, "", "");
      write.disabled = false;
      write.onclick = async () => {
        write.disabled = true;
        setMsg(confirmMsg, "", "Writing…");
        try {
          const result = await post("/setup/integration", { client: item.client, action: "install", token: plan.token });
          await loadIntegration();
          setMsg(agentMsg(item.client), "ok", "Hook written. Backup: " + (result.backup || "none, the file was new") + "."
            + (result.after_install ? " " + result.after_install : ""));
        } catch (error) {
          setMsg(confirmMsg, "error", error.message);
          write.disabled = false;
        }
      };
      write.focus();
    } catch (error) {
      setMsg(msg, "error", error.message);
    } finally {
      start.disabled = false;
    }
  }
  start.addEventListener("click", openPreview);
  cancel.addEventListener("click", () => { confirm.hidden = true; start.hidden = false; start.focus(); });
  hookControls[item.client] = () => { card.scrollIntoView({ block: "start" }); if (!start.hidden) openPreview(); };

  const [browserTone, browserLabel, browserText] = browserState(item);
  const browser = row("Browser control", pill(browserTone, browserLabel), browserText);
  if (item.mcp_url && !["registered", "kept"].includes((item.browser_control || {}).state)) {
    const retry = button("Set up", "", async () => {
      retry.disabled = true;
      setMsg(msg, "", "Checking whether agents can drive a browser here…");
      try {
        const result = await post("/setup/integration", { client: item.client, action: "browser_control" });
        await loadIntegration();
        setMsg(agentMsg(item.client), "ok", browserState({ browser_control: result.browser_control })[2]);
      } catch (error) {
        setMsg(msg, "error", error.message);
        retry.disabled = false;
      }
    });
    browser.side.appendChild(retry);
  }
  msg.dataset.client = item.client;
  card.append(head, msg, mcp.el, hook.el, browser.el);
  return card;
}
function agentMsg(client) { return document.querySelector(`#agents-list .msg[data-client="${client}"]`); }

function renderAgents() {
  const list = $("agents-list");
  list.replaceChildren(...loaded.integration.clients.map(agentCard));
}

function bars(entries) {
  const list = node("ul", "bars");
  const top = Math.max(1, ...entries.map(([, count]) => count));
  for (const [name, count] of entries) {
    const item = node("li");
    const bar = node("span", "bar");
    const fill = node("span");
    fill.style.width = Math.round(count / top * 100) + "%";
    bar.appendChild(fill);
    item.append(node("span", "name", name), bar, node("span", "num", String(count)));
    list.appendChild(item);
  }
  return list;
}
function tile(value, label) {
  const el = node("div", "tile");
  el.append(node("div", "value", String(value)), node("div", "label", label));
  return el;
}
function renderRedirects(stats) {
  const card = $("redirects-card");
  card.replaceChildren(node("h3", "", "Native searches redirected"),
    node("p", "lede", "Last " + stats.days + " days. After each redirect, Smart Tool looks at the same session's next call to see whether the agent followed it."));
  if (!stats.total) { card.appendChild(node("p", "", "No native search was redirected in the last " + stats.days + " days.")); return; }
  const out = stats.outcomes, tokens = stats.tokens_per_call;
  const tiles = node("div", "tiles");
  tiles.append(tile(stats.total, "redirected"), tile(out.followed || 0, "then used smart_search"),
    tile(out.native || 0, "then ran an allowed native call"), tile(out.blocked_again || 0, "blocked again"),
    tile(out.no_other_call || 0, "no other call within 15 min"));
  card.appendChild(tiles);
  if (out.unknown) card.appendChild(node("p", "hint", plural(out.unknown, "redirect") + " had no session logged, so their next call is unknown."));
  const byTool = Object.entries(stats.by_tool).sort((a, b) => b[1] - a[1]);
  if (byTool.length) card.append(node("h4", "", "By tool"), bars(byTool));
  const byReason = Object.entries(stats.by_reason).sort((a, b) => b[1] - a[1]);
  if (byReason.length) card.append(node("h4", "", "By reason"), bars(byReason));
  card.appendChild(node("p", "hint", "Context per call, measured over " + plural(tokens.measured, "call") + ": a native call of the kinds redirected costs about "
    + tokens.native_redirected + " tokens; a smart_search round trip about " + tokens.smart_search
    + " tokens. A redirect saves context only when one smart_search replaces several native searches and reads; that part is not measured yet."));
}

const PATTERN_PAGE_SIZE = 8;
const patternView = { page: 0, filter: "", sort: "impact", selected: new Set(), open: false };
let lastPatterns = null;
function patternPage(items, view) {
  const needle = view.filter.trim().toLowerCase();
  const matched = items.filter((item) => !needle || [item.tool, item.pattern, item.reason, item.example]
    .some((text) => String(text || "").toLowerCase().includes(needle)));
  const sorted = matched.slice().sort(view.sort === "newest"
    ? (a, b) => (b.proposed_at || 0) - (a.proposed_at || 0)
    : (a, b) => (b.history_matches || 0) - (a.history_matches || 0) || (b.proposed_at || 0) - (a.proposed_at || 0));
  const pages = Math.max(1, Math.ceil(sorted.length / PATTERN_PAGE_SIZE));
  const page = Math.min(Math.max(0, view.page), pages - 1);
  return { rows: sorted.slice(page * PATTERN_PAGE_SIZE, (page + 1) * PATTERN_PAGE_SIZE), page, pages,
           matched: sorted.length, total: items.length,
           calls: items.reduce((sum, item) => sum + (item.history_matches || 0), 0) };
}
async function decidePatterns(ids, decision, label) {
  try {
    const result = await post("/setup/integration", { action: "block_patterns", ids, decision });
    for (const id of ids) patternView.selected.delete(id);
    loaded.integration = result;
    renderPatterns(result.block_patterns);
    renderOverview();
    setMsg("patterns-msg", "ok", label + ": " + plural(ids.length, "pattern") + ".");
  } catch (error) {
    setMsg("patterns-msg", "error", error.message);
  }
}
function patternRow(item, actions, selectable) {
  const entry = node("li", "pattern-row" + (selectable ? "" : " plain"));
  if (selectable) {
    const check = node("input");
    check.type = "checkbox";
    check.checked = patternView.selected.has(item.id);
    check.setAttribute("aria-label", "Select " + item.pattern);
    check.addEventListener("change", () => {
      if (check.checked) patternView.selected.add(item.id); else patternView.selected.delete(item.id);
      renderPatterns(lastPatterns);
    });
    entry.appendChild(check);
  }
  const body = node("details", "pattern-body");
  const summary = node("summary");
  const code = node("code", "pattern-code", item.pattern);
  code.title = item.pattern;
  summary.append(node("span", "pattern-tool", item.tool), code);
  const detail = node("div", "pattern-detail");
  detail.append(node("p", "", item.reason), node("p", "hint mono", "Example: " + item.example), node("p", "hint mono", item.pattern));
  body.append(summary, detail);
  const reach = node("span", "pattern-reach", item.history_total ? "would block " + item.history_matches + " of " + item.history_total + " logged " + item.tool + " calls" : "no logged calls to compare");
  const buttons = node("span", "actions");
  buttons.style.marginTop = "0";
  for (const [decision, label] of actions) buttons.appendChild(button(label, "small", () => decidePatterns([item.id], decision, label)));
  const meta = node("div", "pattern-meta");
  meta.append(reach, buttons);
  const main = node("div");
  main.append(body, meta);
  entry.appendChild(main);
  return entry;
}
function renderPatterns(patterns) {
  lastPatterns = patterns;
  const box = $("patterns-list");
  box.replaceChildren();
  if (patterns.error) { box.appendChild(node("p", "msg error", patterns.error)); return; }
  const proposals = patterns.proposals;
  const live = new Set(proposals.map((item) => item.id));
  for (const id of [...patternView.selected]) if (!live.has(id)) patternView.selected.delete(id);
  if (!proposals.length) box.appendChild(node("p", "", "No proposal waiting for you."));
  else {
    const view = patternPage(proposals, patternView);
    patternView.page = view.page;
    box.appendChild(node("p", "", plural(proposals.length, "proposal") + " waiting. Together they would have blocked " + plural(view.calls, "logged call") + ". Open a row for its reason and an example."));
    const tools = node("div", "pattern-tools");
    const filter = node("input");
    filter.type = "text";
    filter.placeholder = "Filter by pattern, reason or example";
    filter.value = patternView.filter;
    filter.setAttribute("aria-label", "Filter proposed patterns");
    filter.addEventListener("input", () => {
      patternView.filter = filter.value;
      patternView.page = 0;
      renderPatterns(lastPatterns);
      const again = document.querySelector(".pattern-tools input[type=text]");
      if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
    });
    const sort = node("select");
    sort.setAttribute("aria-label", "Order of proposed patterns");
    for (const [value, label] of [["impact", "Most blocked calls first"], ["newest", "Newest first"]]) sort.appendChild(new Option(label, value));
    sort.value = patternView.sort;
    sort.addEventListener("change", () => { patternView.sort = sort.value; patternView.page = 0; renderPatterns(lastPatterns); });
    tools.append(filter, sort);
    const bulk = node("div", "actions");
    const pageIds = view.rows.map((item) => item.id);
    const allOnPage = pageIds.length > 0 && pageIds.every((id) => patternView.selected.has(id));
    const selectPage = button(allOnPage ? "Clear this page" : "Select this page", "", () => {
      for (const id of pageIds) if (allOnPage) patternView.selected.delete(id); else patternView.selected.add(id);
      renderPatterns(lastPatterns);
    });
    selectPage.disabled = !pageIds.length;
    const count = patternView.selected.size;
    const accept = button("Accept selected (" + count + ")", "primary", () => decidePatterns([...patternView.selected], "accept", "Accepted"));
    const reject = button("Reject selected (" + count + ")", "", () => decidePatterns([...patternView.selected], "reject", "Rejected"));
    accept.disabled = reject.disabled = !count;
    bulk.append(selectPage, accept, reject);
    const list = node("ul", "pattern-list");
    for (const item of view.rows) list.appendChild(patternRow(item, [["accept", "Accept"], ["reject", "Reject"]], true));
    if (!view.rows.length) list.appendChild(node("li", "hint", "No proposal matches the filter."));
    const pager = node("div", "actions pager");
    const prev = button("Previous", "", () => { patternView.page -= 1; renderPatterns(lastPatterns); });
    const next = button("Next", "", () => { patternView.page += 1; renderPatterns(lastPatterns); });
    prev.disabled = view.page === 0;
    next.disabled = view.page >= view.pages - 1;
    const first = view.page * PATTERN_PAGE_SIZE;
    const shown = view.matched ? (first + 1) + "–" + (first + view.rows.length) : "0";
    const filtered = view.matched < view.total ? " (filtered from " + view.total + ")" : "";
    pager.append(prev, node("span", "hint", shown + " of " + view.matched + filtered + " · page " + (view.page + 1) + " of " + view.pages), next);
    box.append(tools, bulk, list, pager);
  }
  if (patterns.accepted.length) {
    const blocking = node("details", "more");
    blocking.open = patternView.open;
    blocking.addEventListener("toggle", () => { patternView.open = blocking.open; });
    blocking.appendChild(node("summary", "", "Patterns you accepted, blocking now (" + patterns.accepted.length + ")"));
    const list = node("ul", "pattern-list");
    for (const item of patterns.accepted) list.appendChild(patternRow(item, [["remove", "Remove"]], false));
    blocking.appendChild(list);
    box.appendChild(blocking);
  }
  if (patterns.rejected) box.appendChild(node("p", "hint", plural(patterns.rejected, "rejected proposal") + " not offered again."));
}

async function loadIntegration() {
  try {
    loaded.integration = await getJSON("/setup/integration");
    delete failed.integration;
    renderAgents();
    renderRules();
    renderRedirects(loaded.integration.redirects);
    renderPatterns(loaded.integration.block_patterns);
  } catch (error) {
    failed.integration = "Agent status unavailable: " + error.message;
    setMsg("agents-msg", "error", failed.integration);
    $("rules-card").replaceChildren(node("p", "msg error", failed.integration));
    $("redirects-card").replaceChildren(node("p", "msg error", failed.integration));
  }
  renderOverview();
}

async function loadAutostart() {
  try {
    loaded.autostart = await getJSON("/setup/autostart");
    const on = loaded.autostart.installed;
    setPill("autostart-pill", on ? "ok" : "idle", on ? "On, via " + (loaded.autostart.method === "task" ? "Scheduled Task" : "Startup folder") : "Off");
    const toggle = $("btn-autostart-toggle");
    toggle.disabled = false;
    toggle.className = on ? "" : "primary";
    toggle.textContent = on ? "Turn off" : "Turn on";
  } catch (error) {
    setPill("autostart-pill", "bad", "Status unavailable");
    setMsg("autostart-msg", "error", error.message);
  }
  renderOverview();
}
$("btn-autostart-toggle").addEventListener("click", async () => {
  const action = loaded.autostart.installed ? "remove" : "install";
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

const PROVIDER_STATES = { keyless: ["ok", "Free tier, no key"], configured: ["ok", "Your key"], unreadable: ["bad", "Key unreadable"] };
function providerRow(name, info) {
  const entry = node("div", "provider-row");
  const [tone, label] = PROVIDER_STATES[info.status] || ["bad", info.status];
  const title = node("div", "row-title");
  title.append(node("span", "", info.label), pill(tone, label));
  const text = node("div", "row-text");
  text.append(node("div", "", "Without a key: " + info.free), node("div", "", "With your key: " + info.keyed));
  if (info.status === "unreadable") text.appendChild(node("div", "msg error", "The saved key cannot be opened in this Windows profile; paste it again."));
  const side = node("div", "row-side");
  const form = node("div", "key-form");
  form.hidden = true;
  const field = node("div", "field");
  const input = node("input");
  input.type = "password";
  input.autocomplete = "new-password";
  input.id = name + "-key";
  input.placeholder = info.key_prefix ? `Paste the key (starts with ${info.key_prefix})` : "Paste the key from your account";
  const fieldLabel = node("label", "", info.label + " API key");
  fieldLabel.htmlFor = input.id;
  const signup = node("a", "", "Create a key in your " + info.label + " account");
  signup.href = info.signup;
  signup.target = "_blank";
  signup.rel = "noopener noreferrer";
  const hint = node("span", "hint");
  hint.appendChild(signup);
  field.append(fieldLabel, input, hint);
  const msg = node("div", "msg");
  msg.setAttribute("role", "status");
  const save = button("Test and save", "primary", async () => {
    if (!input.value.trim()) { setMsg(msg, "error", "Paste the API key before saving."); return; }
    save.disabled = true;
    setMsg(msg, "", "Testing the key with a real search…");
    try {
      const data = await post("/setup/providers", { provider: name, action: "save", api_key: input.value.trim() });
      await loadProviders(name, `Key accepted (${plural(data.validated_results, "result")} in the test) and protected on this Windows machine.`);
    } catch (error) {
      setMsg(msg, "error", error.message);
      save.disabled = false;
    }
  });
  const formActions = node("div", "actions");
  formActions.append(save, button("Cancel", "", () => { form.hidden = true; open.hidden = false; }));
  form.append(field, formActions);
  const open = button(info.status === "keyless" ? "Add key" : "Replace key", "", () => { form.hidden = false; open.hidden = true; input.focus(); });
  side.appendChild(open);
  if (info.status !== "keyless") {
    const remove = button("Remove key", "", async () => {
      remove.disabled = true;
      setMsg(msg, "", "Removing…");
      try {
        await post("/setup/providers", { provider: name, action: "remove" });
        await loadProviders(name, "Key removed; the free tier is active.");
      } catch (error) {
        setMsg(msg, "error", error.message);
        remove.disabled = false;
      }
    });
    side.appendChild(remove);
  }
  msg.dataset.provider = name;
  entry.append(title, side, text, form, msg);
  return entry;
}
async function loadProviders(provider, message) {
  try {
    loaded.providers = await getJSON("/setup/providers");
    delete failed.providers;
    $("provider-list").replaceChildren(...Object.entries(loaded.providers.providers).map(([name, info]) => providerRow(name, info)));
    if (provider) setMsg(document.querySelector(`#provider-list .msg[data-provider="${provider}"]`), "ok", message);
    if (document.activeElement !== $("contact-input")) $("contact-input").value = loaded.providers.contact;
    $("contact-hint").textContent = "Sent as: " + loaded.providers.user_agent;
  } catch (error) {
    failed.providers = "Provider status unavailable: " + error.message;
    $("provider-list").replaceChildren(node("p", "msg error", failed.providers));
  }
  renderOverview();
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

const AUDIT_SEVERITY = { error: ["bad", "Error"], warn: ["warn", "Warning"], info: ["idle", "Note"] };
function auditWhen(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "never";
}
function renderAudit(report) {
  const box = $("audit-report");
  box.replaceChildren();
  if (!report) {
    box.appendChild(node("p", "hint", "Not audited yet: the next scheduled run covers it, or press Run now."));
    return;
  }
  const s = report.summary;
  box.appendChild(node("p", "", plural(s.error, "error") + ", " + plural(s.warn, "warning") + ", " + plural(s.info, "note") + " · "
    + (report.trigger === "schedule" ? "scheduled run" : "run on request") + " at " + auditWhen(report.checked_at)));
  const changes = report.changes;
  if (changes) box.appendChild(node("p", "hint", "Since the previous run: " + changes.new.length + " new, "
    + changes.resolved.length + " resolved" + (changes.snapshot_changed ? " (configuration changed)." : ".")));
  const list = node("ul", "audit-list");
  for (const finding of report.findings) {
    const entry = node("li", "audit-row");
    const head = node("div", "audit-head");
    const [tone, label] = AUDIT_SEVERITY[finding.severity] || ["idle", finding.severity];
    head.append(pill(tone, label), node("span", "pattern-tool", finding.harness), node("strong", "", finding.subject));
    if (finding.new) head.appendChild(node("span", "new-tag", "new"));
    const detail = node("p", "audit-detail", finding.detail + " ");
    if ((finding.source || "").startsWith("https://")) {
      const link = node("a", "", "documentation");
      link.setAttribute("href", finding.source);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      detail.appendChild(link);
    }
    entry.append(head, detail);
    list.appendChild(entry);
  }
  if (report.findings.length) box.appendChild(list);
  else box.appendChild(node("p", "msg ok", "No collision or broken piece found."));
  if (changes && changes.resolved.length) {
    const resolved = node("details", "more");
    resolved.appendChild(node("summary", "", "Resolved since the previous run (" + changes.resolved.length + ")"));
    const items = node("ul", "audit-list");
    for (const finding of changes.resolved) items.appendChild(node("li", "audit-detail", finding.subject + ": " + finding.detail));
    resolved.appendChild(items);
    box.appendChild(resolved);
  }
  const costs = [];
  for (const side of ["claude", "codex"]) {
    const servers = ((report[side] || {}).mcp || []).filter((m) => m.tools !== undefined);
    if (servers.length) costs.push(side + ": " + servers.map((m) => m.name + " " + m.tools + " tools (~"
      + Math.round(m.tool_chars / 4).toLocaleString() + " tokens)").join(", "));
  }
  if (costs.length) box.appendChild(node("p", "hint", "MCP tools offered — " + costs.join(" · ")));
  const unverified = node("details", "more");
  unverified.appendChild(node("summary", "", "Not verified (" + report.unverified.length + ")"));
  const items = node("ul", "audit-list");
  for (const item of report.unverified) items.appendChild(node("li", "audit-detail", item));
  unverified.appendChild(items);
  box.appendChild(unverified);
}
async function showAudit(root) {
  try {
    renderAudit((await post("/setup/audit", { action: "report", project_root: root })).report);
  } catch (error) {
    setMsg("audit-msg", "error", error.message);
  }
}
async function loadAudit() {
  try {
    const data = await post("/setup/audit", { action: "index" });
    loaded.audit = data;
    delete failed.audit;
    const select = $("sel-audit-project");
    select.replaceChildren(...data.projects.map((root) => {
      const entry = data.index[root];
      return new Option(root + (entry ? " — " + plural(entry.summary.error, "error") + ", " + plural(entry.summary.warn, "warning") : " — not audited yet"), root);
    }));
    $("audit-hint").textContent = data.projects.length ? "Runs on its own every " + Math.round(data.schedule_s / 60) + " minutes." : "Register a project on the Projects page to audit it.";
    $("btn-audit-run").disabled = !data.projects.length;
    if (data.projects.length) {
      const worst = data.projects.slice().sort((a, b) => ((data.index[b] || { summary: { error: -1 } }).summary.error)
        - ((data.index[a] || { summary: { error: -1 } }).summary.error))[0];
      select.value = worst;
      await showAudit(worst);
    }
  } catch (error) {
    failed.audit = "Setup audit unavailable: " + error.message;
    setMsg("audit-msg", "error", failed.audit);
  }
  renderOverview();
}
async function loadAuditHook() {
  try {
    const hook = await post("/setup/audit", { action: "hook_state" });
    $("audit-hook-file").textContent = hook.file;
    const on = hook.state === "current";
    const pillEl = $("audit-hook-pill");
    pillEl.textContent = on ? "On" : hook.state === "outdated" ? "Outdated" : "Off";
    pillEl.dataset.tone = on ? "ok" : hook.state === "outdated" ? "warn" : "idle";
    $("btn-audit-hook-on").hidden = on;
    $("btn-audit-hook-on").textContent = hook.state === "outdated" ? "Update" : "Turn on";
    $("btn-audit-hook-off").hidden = hook.state === "missing";
  } catch (error) {
    setMsg("audit-hook-msg", "error", "Session-start report state unavailable: " + error.message);
  }
}
$("btn-audit-hook-on").addEventListener("click", async () => {
  setMsg("audit-hook-msg", "", "Preparing the change…");
  try {
    const plan = await post("/setup/audit", { action: "hook_preview" });
    setMsg("audit-hook-msg", "", "");
    $("audit-hook-where").textContent = "Adds this entry under " + plan.event + " in " + plan.file + ".";
    $("audit-hook-preview").textContent = JSON.stringify(plan.change, null, 2);
    $("audit-hook-confirm").hidden = false;
    $("btn-audit-hook-write").onclick = async () => {
      $("btn-audit-hook-write").disabled = true;
      try {
        const result = await post("/setup/audit", { action: "hook_install", token: plan.token });
        $("audit-hook-confirm").hidden = true;
        await loadAuditHook();
        setMsg("audit-hook-msg", "ok", "Turned on. Backup: " + (result.backup || "none, the file was new") + ".");
      } catch (error) {
        setMsg("audit-hook-msg", "error", error.message);
      } finally {
        $("btn-audit-hook-write").disabled = false;
      }
    };
  } catch (error) {
    setMsg("audit-hook-msg", "error", error.message);
  }
});
$("btn-audit-hook-cancel").addEventListener("click", () => { $("audit-hook-confirm").hidden = true; });
$("btn-audit-hook-off").addEventListener("click", async () => {
  try {
    const result = await post("/setup/audit", { action: "hook_remove" });
    await loadAuditHook();
    setMsg("audit-hook-msg", "ok", "Turned off. Backup: " + (result.backup || "none") + ".");
  } catch (error) {
    setMsg("audit-hook-msg", "error", error.message);
  }
});
$("sel-audit-project").addEventListener("change", () => showAudit($("sel-audit-project").value));
$("btn-audit-run").addEventListener("click", async () => {
  const run = $("btn-audit-run");
  run.disabled = true;
  setMsg("audit-msg", "", "Auditing; MCP servers are being contacted…");
  try {
    renderAudit((await post("/setup/audit", { action: "run", project_root: $("sel-audit-project").value })).report);
    setMsg("audit-msg", "ok", "Audit finished.");
  } catch (error) {
    setMsg("audit-msg", "error", error.message);
  } finally {
    run.disabled = false;
  }
});

renderGateway();
renderOverview();
if (status.gateway.status === "ready") loadModels();
loadLocalModels();
loadIntegration();
loadAutostart();
loadProviders();
loadAudit();
loadAuditHook();
setInterval(refreshStatus, 15000);
</script>
</body>
</html>
"""
