"""Decisão do PreToolUse do Smart Tool, tomada dentro do daemon já aquecido.

O Claude Code chama a rota POST /hooks/pretool como hook `type: "http"` (sem processo por chamada); o Codex usa o
hook de comando `hooks/pretool_router.py`, que só repassa o payload para a mesma rota.

INVARIANTE: a saída só pode ser `{}` (neutra) ou `deny`. `permissionDecision: "allow"` pula a confirmação do usuário
no Claude Code, e nenhuma decisão aqui é sobre periculosidade, só sobre custo de contexto. Diagnóstico vai em
`systemMessage`, que não altera permissão.
"""
import json
import os

import config
import endpoint_sync
import router

WEB_TOOLS = ("WebSearch", "WebFetch")
SMART_TOOL_SERVER = "smart-tool"
REASON_MAX_CHARS = 200


def clean_reason(reason):
    """The reason is free LLM text fed to the host model: one line, bounded (repository content reaches the router
    prompt through tool_input)."""
    text = " ".join(str(reason or "").split())
    return text[:REASON_MAX_CHARS] or "call broad enough to be worth an indexed search"


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def smart_tool_in_session(cwd):
    """Claude Code loads MCP servers from ~/.claude.json (user and per project) and the project's .mcp.json."""
    with open(endpoint_sync.claude_json(), encoding="utf-8") as stream:
        data = json.load(stream)
    key = os.path.normcase(os.path.normpath(cwd or ""))
    project = next((value for path, value in (data.get("projects") or {}).items()
                    if os.path.normcase(os.path.normpath(path)) == key), {})
    if SMART_TOOL_SERVER in (project.get("disabledMcpServers") or []):
        return False
    if SMART_TOOL_SERVER in (data.get("mcpServers") or {}) or SMART_TOOL_SERVER in (project.get("mcpServers") or {}):
        return True
    try:
        with open(os.path.join(cwd or "", ".mcp.json"), encoding="utf-8") as stream:
            return SMART_TOOL_SERVER in (json.load(stream).get("mcpServers") or {})
    except (OSError, ValueError):
        return False


def advise(reason):
    """Same routing advice without blocking: the agent sees it next to the tool result and decides next time."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": f"Smart Tool tip (the call was not blocked): {reason}"}}


def decide(payload, client, web_route):
    """Hook output for one PreToolUse payload, following config hook_mode (redirect | advise | off)."""
    cfg = config.load_config()
    mode = config.hook_mode(cfg)
    if mode == "off" or not isinstance(payload, dict):
        return {}
    output = _route(payload, client, web_route, cfg)
    reason = (output.get("hookSpecificOutput") or {}).get("permissionDecisionReason")
    return advise(reason) if mode == "advise" and reason else output


def _route(payload, client, web_route, cfg):
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = payload.get("cwd")
    router_model = cfg.get("router_model")
    if not router_model:
        return {}
    if tool_name in WEB_TOOLS:
        try:
            present = smart_tool_in_session(cwd)
        except (OSError, ValueError):
            present = False
        if not present:
            return {}
        decision = web_route(tool_name, str(tool_input.get("url") or ""))
        return deny(clean_reason(decision.get("reason"))) if decision.get("redirect") else {}
    router.CLIENT = client
    try:
        decision, reason = router.decide(tool_name, tool_input, router_model, cwd=cwd)
    except Exception as exc:
        try:
            router.log_unavailable(tool_name, tool_input, exc)
        except Exception:
            pass
        return {"systemMessage": f"Smart Tool unavailable, routing skipped: {type(exc).__name__}"}
    if decision == "redirect":
        return deny(f"Use smart_search instead of raw {tool_name}: {clean_reason(reason)}")
    return {}
