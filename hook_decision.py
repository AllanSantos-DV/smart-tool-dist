"""Decisão do PreToolUse do Smart Tool, tomada dentro do daemon já aquecido.

O Claude Code chama a rota POST /hooks/pretool como hook `type: "http"` (sem processo por chamada); o Codex usa o
hook de comando `hooks/pretool_router.py`, que só repassa o payload para a mesma rota.

INVARIANTE: a saída só pode ser `{}` (neutra) ou `deny`. `permissionDecision: "allow"` pula a confirmação do usuário
no Claude Code, e nenhuma decisão aqui é sobre periculosidade, só sobre custo de contexto. Diagnóstico vai em
`systemMessage`, que não altera permissão.
"""
import json
import os

import code_graph
import code_impact
import config
import doc_check
import duplicates
import edit_preview
import index_profile
import index_scope
import endpoint_sync
import project_store
import router

WEB_TOOLS = ("WebSearch", "WebFetch")
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "apply_patch")
MAX_DOC_FINDINGS = 5
MAX_DUPLICATE_FINDINGS = 3
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


def _project_of(path):
    target = os.path.normcase(os.path.abspath(path))
    roots = [p["root"] for p in project_store.all_projects()
             if target.startswith(os.path.normcase(os.path.abspath(p["root"])) + os.sep)]
    return max(roots, key=len) if roots else None


def _impact_line(root, rel, name):
    """Callers and tests from the code graph, only when its analysis is already cached: the hook never waits for one."""
    if not root or code_graph.cached_symbols(root)[0] is None:
        return ""
    try:
        found = code_impact.impact(root, f"{rel}::{name}", limit=3)
    except ValueError:
        return ""
    counts = found["counts"]
    tests = ", ".join(f"{t['function']} ({t['path']})" for t in found["tests"])
    calls = f"; tests calling it: {tests}" if tests else ""
    return f" {counts['callers']} caller(s){calls}; project_manage affected_tests lists every test to run after the edit."


def _doc_lines(findings):
    lines = []
    for root, rel, item in findings[:MAX_DOC_FINDINGS]:
        if item["issue"] == "missing":
            text = f"{item['name']} ({rel}:{item['start']}) has no docstring: write one (purpose, parameters, return)."
        else:
            text = (f"{item['name']} ({rel}:{item['start']}) changed its signature: update its docstring if the "
                    f"contract changed.")
        lines.append(text + _impact_line(root, rel, item["name"]))
    if len(findings) > MAX_DOC_FINDINGS:
        lines.append(f"{len(findings) - MAX_DOC_FINDINGS} more function(s) in the same situation.")
    return "Smart Tool docs: " + " ".join(lines)


def _duplicate_lines(findings):
    lines = []
    for rel, item in findings[:MAX_DUPLICATE_FINDINGS]:
        existing = item["existing"]
        where = f"{existing['name']} in {existing['path']}:{existing['lines']}"
        if item["type"] == "exact":
            lines.append(f"{item['name']} ({rel}:{item['lines']}) has the same body as {where}: reuse it instead of a copy.")
        else:
            lines.append(f"{item['name']} ({rel}:{item['lines']}) is near-identical to {where} (score {item['score']}, "
                         f"local names ignored): check whether it can be reused.")
    if len(findings) > MAX_DUPLICATE_FINDINGS:
        lines.append(f"{len(findings) - MAX_DUPLICATE_FINDINGS} more copied function(s).")
    return ("Smart Tool duplicates: " + " ".join(lines)
            + " If the copy is intentional (moving the function, a deliberate fork), go ahead.")


def _edit_review(payload, doc_mode, duplicate_mode):
    """Checks of an edit before it runs. Docstrings follow doc_mode, in code files of registered projects only (tests,
    docs and files outside a project are left out, as in the docs coverage): missing ones deny the edit in require mode and become a reminder in remind mode; a
    documented function whose signature changed always gets a reminder. Copies of indexed functions follow
    duplicate_mode and only add context, never deny."""
    doc_findings, duplicate_findings, failures = [], [], []
    for path, before, after in edit_preview.preview(payload.get("tool_name"), payload.get("tool_input"), payload.get("cwd")):
        if not doc_check.supported(path):
            continue
        root = _project_of(path)
        rel = os.path.relpath(path, root).replace(os.sep, "/") if root else os.path.basename(path)
        edited = doc_check.written(path, before, after)
        profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile")) if root else None
        if doc_mode != "off" and root and index_profile.kind(rel, profile) == "code":
            doc_findings += [(root, rel, item) for item in doc_check.review(path, before, after, edited)]
        if duplicate_mode != "off" and root and edited["touched"]:
            try:
                duplicate_findings += [(rel, item) for item in duplicates.on_write(root, path, before, after, edited)]
            except (ValueError, RuntimeError) as exc:
                failures.append(f"Smart Tool duplicates check skipped: {exc}")
    parts = ([_doc_lines(doc_findings)] if doc_findings else []) + \
        ([_duplicate_lines(duplicate_findings)] if duplicate_findings else []) + failures
    if not parts:
        return {}
    message = " ".join(parts)
    if doc_mode == "require" and any(item["issue"] == "missing" for _root, _rel, item in doc_findings):
        return deny(message + " Redo the edit with the docstring.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}


def decide(payload, client, web_route):
    """Hook output for one PreToolUse payload: edits follow doc_mode (require | remind | off) and duplicate_mode
    (warn | off), the other tools follow hook_mode (redirect | advise | off)."""
    cfg = config.load_config()
    if isinstance(payload, dict) and payload.get("tool_name") in EDIT_TOOLS:
        doc_mode, duplicate_mode = config.doc_mode(cfg), config.duplicate_mode(cfg)
        if doc_mode == "off" and duplicate_mode == "off":
            return {}
        return _edit_review(payload, doc_mode, duplicate_mode)
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
    router.CLIENT.set(client)
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
