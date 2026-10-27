"""Decisão do PreToolUse e do PostToolUse do Smart Tool, tomada dentro do daemon já aquecido.

O Claude Code chama as rotas POST /hooks/pretool e /hooks/posttool como hooks `type: "http"` (sem processo por
chamada); o Codex usa o hook de comando `hooks/pretool_router.py`, que só repassa o payload para /hooks/pretool.

Uma busca nativa que a regra redireciona, e WebFetch/WebSearch, não são negadas: o Smart Tool responde no lugar e o
texto volta como saída da própria chamada (`tool_redirect`). INVARIANTE: a saída do PreToolUse é `{}`, `updatedInput`
sem `permissionDecision` (as regras de permissão do usuário julgam a chamada reescrita), `allow` só junto do comando
que o próprio hook escreveu para imprimir a resposta no Codex (`tool_redirect.is_codex_command`), ou `deny` quando a
chamada não dá para montar. Nenhuma decisão aqui é sobre periculosidade, só sobre custo de contexto. Diagnóstico vai em
`systemMessage`, que não altera permissão.
"""
import os

import affected_tests
import code_graph
import code_impact
import config
import dependency_notice
import doc_check
import duplicates
import edit_preview
import index_profile
import index_scope
import endpoint_sync
import project_store
import redirect_rule
import router
import tool_redirect

WEB_TOOLS = ("WebSearch", "WebFetch")
# A warm smart_search takes 4 to 13 s with the reranker, and in Codex a late answer is a lost one (no output
# replacement): a redirected shell search waits this long within the daemon's HOOK_SHELL_BUDGET_S. The PostToolUse
# entry has its own client timeout (60 s).
PRE_WAIT_S = 10.0
SEARCH_POST_WAIT_S = 15
WEB_POST_WAIT_S = 50
SINGLE_LANGUAGE_REASON = "redirected native search: its pattern is the only query there is"
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "apply_patch")
MAX_DOC_FINDINGS = 5
MAX_DUPLICATE_FINDINGS = 3
SMART_TOOL_SERVER = "smart-tool"
SMART_SEARCH_TOOL = f"mcp__{SMART_TOOL_SERVER}__smart_search"
REASON_MAX_CHARS = 200


def clean_reason(reason):
    """The reason is free LLM text fed to the host model: one line, bounded (repository content reaches the router
    prompt through tool_input)."""
    text = " ".join(str(reason or "").split())
    return text[:REASON_MAX_CHARS] or "call broad enough to be worth an indexed search"


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# Claude Code built-in subagents whose tool list has no MCP tool (claude-code-guide: Glob, Grep, Read, WebFetch,
# WebSearch; statusline-setup: Read, Edit).
BUILTIN_AGENTS_WITHOUT_MCP = frozenset({"claude-code-guide", "statusline-setup"})


def _frontmatter(path):
    """`name` and `tools` of an agent definition (.md with YAML frontmatter); tools is None when the field is absent
    (the agent inherits every tool), else the list of tool names (inline `[a, b]`, `a, b` or a `- a` block)."""
    with open(path, encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    name, tools, block = None, None, False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if block and line.lstrip().startswith("- "):
            tools.append(line.strip()[2:].strip().strip("'\""))
            continue
        block = False
        key, _sep, value = line.partition(":")
        value = value.strip()
        if key == "name":
            name = value.strip("'\"")
        elif key == "tools":
            if value:
                tools = [item.strip().strip("'\"") for item in value.strip("[]").split(",") if item.strip()]
            else:
                tools, block = [], True
    return name, tools


def agent_has_smart_tool(agent_type, cwd):
    """Whether the subagent running the call can call the Smart Tool MCP tools: denying a native tool to an agent that
    has no replacement only makes it give up. Agents defined in .claude/agents (user or project) are read; unknown
    agents keep the redirect."""
    if not agent_type:
        return True
    if agent_type in BUILTIN_AGENTS_WITHOUT_MCP:
        return False
    for folder in (os.path.join(cwd or "", ".claude", "agents"), os.path.join(endpoint_sync.claude_dir(), "agents")):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for entry in names:
            if not entry.endswith(".md"):
                continue
            try:
                name, tools = _frontmatter(os.path.join(folder, entry))
            except (OSError, UnicodeDecodeError):
                continue
            if (name or entry.split(".")[0]) != agent_type:
                continue
            return tools is None or any(tool == "*" or tool.startswith(f"mcp__{SMART_TOOL_SERVER}") for tool in tools)
    return True


def _search_root(target, cwd):
    """project_root for smart_search: the registered project holding the folder the redirected search reads, else the
    session folder when it holds that folder (a subfolder is not indexed on its own), else that folder."""
    target = os.path.abspath(target or cwd or ".")
    key = os.path.normcase(target) + os.sep
    roots = [p["root"] for p in project_store.all_projects()
             if key.startswith(os.path.normcase(os.path.abspath(p["root"])).rstrip(os.sep) + os.sep)]
    if roots:
        return max(roots, key=len)
    session = os.path.abspath(cwd) if cwd else None
    return session if session and key.startswith(os.path.normcase(session).rstrip(os.sep) + os.sep) else target


def _tool_name(client, tool):
    return f"mcp__{SMART_TOOL_SERVER}__{tool}" if client == "claude" else f"the {tool} tool of the {SMART_TOOL_SERVER} MCP server"


def _load_hint(client, tool):
    """Claude Code defers MCP tools behind tool search: a blocked agent may not have the schema loaded yet."""
    if client != "claude":
        return ""
    return f' If it is not in your tool list yet, load it first with ToolSearch, query "select:mcp__{SMART_TOOL_SERVER}__{tool}".'


def redirect_message(client, tool_name, target, reason, cwd):
    """Deny text for a broad raw search: the exact tool and arguments to call (project_root from the folder the search
    reads), and that rewriting the same search in Bash, python, node or PowerShell is the bypass the rule stops. It
    never suggests a way around the rule (narrower or per-file searches): the only way offered is smart_search."""
    root = _search_root(target, cwd).replace("\\", "/")
    return (f"Blocked by Smart Tool, a routing rule set by the user, not a failure ({tool_name}: {clean_reason(reason)}). "
            f'Run this search with {_tool_name(client, "smart_search")}: project_root="{root}", query_identifiers = what '
            f"you are looking for in identifier terms (plus query_comments when the project has two languages)."
            f'{_load_hint(client, "smart_search")} Do not repeat the same search through Bash, python, node or '
            f"PowerShell: that bypasses the rule.")


def web_message(client, tool_name, url, why):
    """Deny text for native WebFetch/WebSearch: the Smart Tool call to make instead, with the URL filled in."""
    if tool_name == "WebFetch":
        call = f'{_tool_name(client, "web_fetch")} with url="{url}" and prompt = what you need from the page'
        tool, fetchers = "web_fetch", "curl, wget, python or PowerShell"
    else:
        call = f"{_tool_name(client, 'web_search')} with query (and query_en in English)"
        tool, fetchers = "web_search", "curl, a browser script or another search tool"
    return (f"Blocked by Smart Tool, a routing rule set by the user, not a failure. Call {call}: {why}"
            f"{_load_hint(client, tool)} Do not fetch it through {fetchers}: that bypasses the rule.")


def advise(reason):
    """Same routing advice without blocking: the agent sees it next to the tool result and decides next time."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": f"Smart Tool tip (the call was not blocked): {reason}"}}


def _project_of(path):
    project = project_store.containing(path)
    return project["root"] if project else None


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
    return (f" {counts['callers']} caller(s){calls}{_run_command(root, found['tests'])}; project_manage affected_tests "
            "lists every test to run after the edit.")


def _run_command(root, tests):
    """`; run them: <command>` for the test files of `tests`, one command per runner and folder, or "" when no runner
    is known for them."""
    commands = affected_tests._commands(root, sorted({t["path"] for t in tests})) if tests else []
    runnable = [f"cd {c['cwd']} && {c['command']}" if c["cwd"] != "." else c["command"] for c in commands if c["command"]]
    return f"; run them: {' ; '.join(runnable)}" if runnable else ""


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


def _edit_review(payload, doc_mode, duplicate_mode, dependency_mode):
    """Checks of an edit before it runs. Docstrings follow doc_mode, in code files of registered projects only (tests,
    docs and files outside a project are left out, as in the docs coverage): missing ones deny the edit in require mode and become a reminder in remind mode; a
    documented function whose signature changed always gets a reminder. Copies of indexed functions follow
    duplicate_mode and only add context, never deny. An edit of a package.json follows dependency_mode: advisories
    of the added or changed dependencies are added as context, never a denial."""
    with code_graph.pending_scope():
        return _edit_review_scoped(payload, doc_mode, duplicate_mode, dependency_mode)


def _dependency_part(before, after):
    """The dependency notice of one package.json edit, or the "dependency check skipped" line when it cannot run.

    Parameters: before (str or None) text before the edit; after (str) text after it.
    Returns: str, empty when there is nothing to say.
    """
    try:
        return dependency_notice.notice(before, after)
    except dependency_notice.DependencyCheckError as exc:
        return f"{dependency_notice.PREFIX}dependency check skipped: {exc}"


def _edit_review_scoped(payload, doc_mode, duplicate_mode, dependency_mode):
    """`_edit_review` inside one `code_graph.pending_scope`, so the pending-changes scan runs once per review."""
    doc_findings, duplicate_findings, failures, dependency_parts = [], [], [], []
    for path, before, after in edit_preview.preview(payload.get("tool_name"), payload.get("tool_input"), payload.get("cwd")):
        if dependency_mode != "off" and os.path.basename(path) == "package.json":
            dependency_parts += [part for part in (_dependency_part(before, after),) if part]
            continue
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
        ([_duplicate_lines(duplicate_findings)] if duplicate_findings else []) + failures + dependency_parts
    if not parts:
        return {}
    message = " ".join(parts)
    if doc_mode == "require" and any(item["issue"] == "missing" for _root, _rel, item in doc_findings):
        return deny(message + " Redo the edit with the docstring.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}


def decide(payload, client, web_route, jobs):
    """Hook output for one PreToolUse payload: edits follow doc_mode (require | remind | off) and duplicate_mode
    (warn | off) and dependency_mode (warn | off), the other tools follow hook_mode (redirect | advise | off). In redirect mode the Smart Tool call that
    replaces a redirected one starts in `jobs` under the call's tool_use_id (see `_redirect`); only a search whose
    query cannot be built, or a client that sends no tool_use_id, is denied with the exact call to make, and never in a
    subagent whose tool list has no Smart Tool MCP tool: it has nothing to switch to."""
    cfg = config.load_config()
    if isinstance(payload, dict) and payload.get("tool_name") in EDIT_TOOLS:
        doc_mode, duplicate_mode, dependency_mode = (config.doc_mode(cfg), config.duplicate_mode(cfg),
                                                     config.dependency_mode(cfg))
        if doc_mode == "off" and duplicate_mode == "off" and dependency_mode == "off":
            return {}
        return _edit_review(payload, doc_mode, duplicate_mode, dependency_mode)
    if isinstance(payload, dict) and str(payload.get("tool_name")).startswith(f"mcp__{SMART_TOOL_SERVER}__"):
        if payload["tool_name"] == SMART_SEARCH_TOOL:
            router.CLIENT.set(client)
            router.SESSION.set(payload.get("session_id"))
            router._log_decision(SMART_SEARCH_TOOL, {}, "allow", "Smart Tool search, logged to measure followed redirects")
        return {}
    mode = config.hook_mode(cfg)
    if mode == "off" or not isinstance(payload, dict):
        return {}
    plan = _route(payload, client, web_route, cfg)
    if "kind" not in plan:
        return plan
    if mode == "advise":
        return advise(plan["message"])
    if plan["kind"] and payload.get("tool_use_id"):
        return _redirect(payload, client, plan, jobs)
    if not agent_has_smart_tool(payload.get("agent_type") if payload.get("agent_id") else None, payload.get("cwd")):
        return {}
    return deny(plan["message"])


def _redirect(payload, client, plan, jobs):
    """Starts the Smart Tool call of a redirected one. A shell search whose answer comes within PRE_WAIT_S is rewritten
    so the answer is all it prints: a no-op whose output the PostToolUse replaces in Claude Code, a `cat` of a file
    holding the answer in Codex (which has no output replacement and applies a rewrite only with `allow`). Anything
    else runs as the agent wrote it and, in Claude Code, `after` swaps its output for the answer."""
    key, tool_name = payload["tool_use_id"], payload["tool_name"]
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    jobs.start(key, plan["kind"], plan["arguments"])
    if tool_name not in tool_redirect.NO_OP:
        return {}
    text = _answer(jobs, key, PRE_WAIT_S, tool_name)
    if text is None:
        return {}
    if client != "codex":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "updatedInput": {**tool_input, "command": tool_redirect.NO_OP[tool_name]}}}
    jobs.forget(key)
    _log_served(client, payload)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "Smart Tool answered this search",
                                   "updatedInput": {**tool_input, "command": tool_redirect.codex_command(text)}}}


def _answer(jobs, key, timeout, tool_name):
    """The Smart Tool text of key within timeout; None when it is not ready or the call failed (logged: the native call
    then runs as written)."""
    try:
        text = jobs.wait(key, timeout)
    except Exception as exc:
        jobs.forget(key)
        router._log_error(f"redirect of {tool_name} not served: {type(exc).__name__}: {exc}")
        return None
    return text if isinstance(text, str) and text.strip() else None


def _log_served(client, payload):
    router.CLIENT.set(client)
    router.SESSION.set(payload.get("session_id"))
    router._log_decision(payload.get("tool_name"), {}, "served", "Smart Tool answer returned as the call's output")


def after(payload, client, jobs):
    """PostToolUse output: the call's own response with the Smart Tool answer started for its tool_use_id in the field
    the model reads (`updatedToolOutput`), or `{}` when there is none, it did not come in time or the response has an
    unknown shape (the native output stays)."""
    if not isinstance(payload, dict) or not payload.get("tool_use_id"):
        return {}
    key, tool_name = payload["tool_use_id"], payload.get("tool_name")
    text = _answer(jobs, key, WEB_POST_WAIT_S if tool_name in WEB_TOOLS else SEARCH_POST_WAIT_S, tool_name)
    jobs.forget(key)
    output = tool_redirect.output_for(tool_name, payload.get("tool_response"), text) if text else None
    if output is None:
        return {}
    _log_served(client, payload)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": output}}


def _route(payload, client, web_route, cfg):
    """`{}` or a `systemMessage` output when the call runs untouched, else the redirect plan: `kind` and `arguments`
    of the Smart Tool call (kind None when no query can be built from the call) and `message`, the exact call for an
    agent that has to make it itself."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = payload.get("cwd")
    if tool_name in WEB_TOOLS:
        if not cfg.get("router_model") or tool_input.get("allowed_domains") or tool_input.get("blocked_domains"):
            return {}
        url = str(tool_input.get("url") or "")
        decision = web_route(tool_name, url)
        if not decision.get("redirect"):
            return {}
        if tool_name == "WebFetch":
            kind, arguments = "web_fetch", {"url": url, "prompt": str(tool_input.get("prompt") or "")}
        else:
            query = str(tool_input.get("query") or "")
            kind, arguments = "web_search", {"query": query, "query_en": query}
        return {"kind": kind if all(arguments.values()) else None, "arguments": arguments,
                "message": web_message(client, tool_name, url, clean_reason(decision.get("reason")))}
    router.CLIENT.set(client)
    router.SESSION.set(payload.get("session_id"))
    try:
        decision, reason, target = router.decide(tool_name, tool_input, cwd=cwd)
    except Exception as exc:
        try:
            router.log_unavailable(tool_name, tool_input, exc)
        except Exception:
            pass
        return {"systemMessage": f"Smart Tool unavailable, routing skipped: {type(exc).__name__}"}
    if decision != "redirect" or (tool_name in tool_redirect.NO_OP and tool_input.get("run_in_background")):
        return {}
    terms = redirect_rule.search_terms(tool_name, tool_input)
    arguments = {"project_root": _search_root(target, cwd), "query_identifiers": terms,
                 "single_language_reason": SINGLE_LANGUAGE_REASON}
    return {"kind": "smart_search" if terms else None, "arguments": arguments,
            "message": redirect_message(client, tool_name, target, reason, cwd)}
