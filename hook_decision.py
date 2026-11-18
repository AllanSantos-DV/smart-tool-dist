"""Decisão do PreToolUse e do PostToolUse do Smart Tool, tomada dentro do daemon já aquecido.

O Claude Code chama as rotas POST /hooks/pretool e /hooks/posttool como hooks `type: "http"` (sem processo por
chamada); o Codex usa o hook de comando `hooks/pretool_router.py`, que só repassa o payload para /hooks/pretool.

Uma busca nativa que a regra redireciona, e WebFetch/WebSearch, não são negadas: o Smart Tool responde no lugar e o
texto volta como saída da própria chamada (`tool_redirect`). INVARIANTE: a saída do PreToolUse é `{}`, `updatedInput`
sem `permissionDecision` (as regras de permissão do usuário julgam a chamada reescrita), `allow` só junto do comando
que o próprio hook escreveu para imprimir a resposta no Codex (`tool_redirect.is_codex_command`), ou `deny` quando a
edição que a regra de documentação ou de comentário exige refazer (`_edit_review`); uma busca sem `tool_use_id` ou sem
consulta legível nunca é negada: é respondida no contexto (`_answer_in_place`). Diagnóstico vai em `systemMessage`, que
não altera permissão.
"""
import os
import subprocess
import time
import uuid

import affected_tests
import code_graph
import code_impact
import comment_check
import config
import credential_check
import dependency_notice
import doc_check
import doc_scope
import dossier
import duplicates
import edit_preview
import index_profile
import index_scope
import layer_check
import link_check
import endpoint_sync
import project_admission
import project_store
import redirect_rule
import removed_callers
import requester
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
MAX_DECISION_FUNCTIONS = 3
DECISION_TIMEOUT_MS = 300
MAX_SHELL_REVIEW_FILES = 20
MAX_REVIEW_BYTES = 1_000_000
# FAT keeps two-second timestamps: a file the call wrote can look older than the call.
MTIME_SLACK_S = 2.0
SHELL_START_TTL_S = 3600
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


def _indexable(target, cwd, session_id):
    """Whether the folder a search reads may be a Smart Tool project for this session; when not, the native tool runs
    untouched instead of starting an index of a folder that is not the session's project."""
    try:
        project_admission.admit(_search_root(target, cwd), requester.session_origin(session_id))
    except (ValueError, OSError):
        return False
    return True


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


def instruct_message(client, tool_name, target, reason, cwd):
    """Tip for a call matching an accepted pattern in mode instruct: the call runs; the text names the smart_search
    call that answers searches like it, with project_root from the folder the call reads."""
    root = _search_root(target, cwd).replace("\\", "/")
    return (f"This {tool_name} call {clean_reason(reason)}. Searches like it are answered by "
            f'{_tool_name(client, "smart_search")}: project_root="{root}", query_identifiers = what you are looking '
            f"for in identifier terms (plus query_comments when the project has two languages)."
            f'{_load_hint(client, "smart_search")}')


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
        short = item["name"].split(".")[-1]
        if item["issue"] == "missing" and item.get("malformed"):
            text = f"{item['name']} ({rel}:{item['start']}) has a doc comment that does not start with '{short} ': fix it."
        elif item["issue"] == "missing" and item.get("convention") == "go":
            text = f"{item['name']} ({rel}:{item['start']}) has no doc comment: write one starting with '{short} '."
        elif item["issue"] == "missing":
            text = f"{item['name']} ({rel}:{item['start']}) has no docstring: write one (purpose, parameters, return)."
        else:
            text = (f"{item['name']} ({rel}:{item['start']}) changed its signature: update its docstring if the "
                    f"contract changed.")
        lines.append(text + _impact_line(root, rel, item["name"]))
    if len(findings) > MAX_DOC_FINDINGS:
        lines.append(f"{len(findings) - MAX_DOC_FINDINGS} more function(s) in the same situation.")
    return "Smart Tool docs: " + " ".join(lines)


_COMMENT_FIX = {
    "commented_code": "is commented-out code: delete it, version control keeps it",
    "history_tag": "carries a ticket, version or history tag: drop it, that belongs in the commit message",
    "loose_docs": "documents the function below as a loose comment: move it into the docstring in the language's "
                  "standard format",
    "inline_long": "is a long comment inside a function: keep one short line that says why, or move the contract "
                   "into the docstring",
}


def _comment_lines(findings):
    lines = [f"{rel}:{item['line']} {_COMMENT_FIX[item['issue']]}" for rel, item in findings[:MAX_DOC_FINDINGS]]
    if len(findings) > MAX_DOC_FINDINGS:
        lines.append(f"{len(findings) - MAX_DOC_FINDINGS} more comment(s) in the same situation.")
    return "Smart Tool comments: " + "; ".join(lines) + "."


def _scope_lines(findings):
    shown = [f"{item['id']} ({'signature changed' if item['issue'] == 'signature_changed' else 'removed'}, "
             f"{item['file']})" for item in findings[:MAX_DOC_FINDINGS]]
    more = f" and {len(findings) - MAX_DOC_FINDINGS} more" if len(findings) > MAX_DOC_FINDINGS else ""
    return ("Smart Tool docs scope: this edit leaves documented entries out of date: " + "; ".join(shown) + more
            + ". After the edit, regenerate with project_manage action=docs generate=true and review the prose of "
              "those entries.")


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


def _layer_lines(findings):
    lines = [f"`{item['function']}` ({rel}:{item['line']}) now calls `{item['called']}` ({item['path']}), a function of the "
             f"higher layer {item['to']}, from layer {item['from']}" for rel, item in findings]
    return ("Smart Tool layers: " + "; ".join(lines) + ". Calls go down the declared layers (.smart-tool/layers.json); "
            "if this one is intended, go ahead.")


def _callers_lines(answers):
    lines = []
    for answer in answers:
        resigned = "changed" in answer
        for item in answer["findings"][:MAX_DOC_FINDINGS]:
            same = f" A function named {item['name'].rpartition('.')[2]} also exists in {', '.join(item['also_defined_in'])}." \
                if item["also_defined_in"] else ""
            sites = removed_callers.sites_text(item)
            lines.append(f"`{item['name']}` ({item['path']}) has a new signature after this edit and is called from {sites} "
                         f"outside this file: check that the callers pass what it takes now.{same}" if resigned else
                         f"`{item['name']}` ({item['path']}) is gone after this edit but is still called from "
                         f"{sites}: update the callers or keep the name.{same}")
        if not answer["findings"]:
            names = ", ".join(f"`{name}`" for name in answer["changed" if resigned else "removed"])
            lines.append(f"no caller of {names}, whose signature changed, was found." if resigned else
                         f"no caller of the removed {names} was found.")
    notes = " ".join(dict.fromkeys(answer["note"] for answer in answers if answer["note"]))
    return "Smart Tool callers: " + " ".join(lines) + (f" {notes}" if notes else "")


def _credential_lines(findings):
    lines = [f"{rel}:{item['line']} looks like a {item['kind']}" for rel, item in findings[:MAX_DOC_FINDINGS]]
    if len(findings) > MAX_DOC_FINDINGS:
        lines.append(f"{len(findings) - MAX_DOC_FINDINGS} more line(s) in the same situation.")
    return ("Smart Tool credentials: " + "; ".join(lines) + ". Keep it out of the file: read it from an environment "
            "variable or a secret store. If it is a fake value for a test, go ahead.")


def _link_lines(broken, unchecked):
    """The link notes: the links that point to nothing, and the ones that could not be checked with the reason."""
    parts = []
    if broken:
        lines = [f"{rel}:{item['line']} points to `{item['link']}`, which does not exist" for rel, item in broken[:MAX_DOC_FINDINGS]]
        if len(broken) > MAX_DOC_FINDINGS:
            lines.append(f"{len(broken) - MAX_DOC_FINDINGS} more link(s) in the same situation")
        parts.append("Smart Tool links: " + "; ".join(lines) + ". Fix the path, or create the file if it is coming next.")
    if unchecked:
        lines = [f"{rel}{':' + str(item['line']) if item['line'] else ''}{' `' + item['link'] + '`' if item['link'] else ''} "
                 f"({item['why']})" for rel, item in unchecked[:MAX_DOC_FINDINGS]]
        if len(unchecked) > MAX_DOC_FINDINGS:
            lines.append(f"{len(unchecked) - MAX_DOC_FINDINGS} more not checked")
        parts.append("Smart Tool links not checked: " + "; ".join(lines) + ".")
    return parts




def _decision_lines(found):
    """The note that names the decisions recorded about the touched functions, each note once.

    Parameters: found (list of (rel, function name, hits)) the functions with a note and their `dossier.notes_about` hits.
    Returns: str, empty when every note was already said.
    """
    seen, sentences = set(), []
    for rel, name, hits in found:
        fresh = [hit for hit in hits if hit["id"] not in seen]
        seen.update(hit["id"] for hit in fresh)
        if fresh:
            sentences.append(f"`{name}` ({rel}) is named in " + "; ".join(f"`{hit['id']}` {hit['title']}" for hit in fresh))
    return "Smart Tool decision notes: " + ". ".join(sentences) + ". Read them before changing what they record." \
        if sentences else ""


def _decision_findings(root, rel, touched, failures):
    """The recorded decisions about the touched functions of one file, looked up in the project's notes (not its commit
    messages: reading them runs a `git log` the budget cannot cut) within `DECISION_TIMEOUT_MS` each.

    Parameters: root (str) project folder; rel (str) path of the file relative to it; touched (list) the functions
    `doc_check.written` found touched; failures (list) where a search that fails or is cut adds its line.
    Returns: list of (rel, function name, hits) for the functions that a note or commit message names.
    """
    found = []
    for function in touched:
        try:
            answer = dossier.notes_about(root, rel, function["name"], timeout_ms=DECISION_TIMEOUT_MS, history=False)
        except (OSError, ValueError, RuntimeError) as exc:
            failures.append(f"Smart Tool decision notes check skipped: {exc}")
            break
        line = f"Smart Tool decision notes search cut: {'; '.join(answer['reasons'])}"
        if answer["reasons"] and line not in failures:
            failures.append(line)
        if answer["hits"]:
            found.append((rel, function["name"], answer["hits"]))
    return found


def _edit_review(payload, doc_mode, duplicate_mode, dependency_mode, comment_mode="off", layer_mode="off",
                 link_mode="off", decision_mode="off"):
    """Checks of an edit before it runs. Docstrings follow doc_mode, in code files of registered projects only (tests,
    docs and files outside a project are left out, as in the docs coverage): missing ones deny the edit in require mode and become a reminder in remind mode; a
    documented function whose signature changed always gets a reminder. Comments the edit adds in the wrong place
    (see comment_check) follow comment_mode in the same files: require denies, remind adds context. Copies of indexed
    functions follow duplicate_mode and only add context, never deny. An edit of a package.json, requirements*.txt or go.mod follows dependency_mode: advisories
    of the added or changed dependencies are added as context, never a denial. Local links an edit of an HTML or Markdown
    file adds or changes follow link_mode: missing targets and links that cannot be checked are added as context. The notes
    and commit messages that name a touched function follow decision_mode and only add context."""
    with code_graph.pending_scope():
        return _edit_review_scoped(payload, doc_mode, duplicate_mode, dependency_mode, comment_mode, layer_mode, link_mode,
                                   decision_mode)


def _dependency_part(before, after, ecosystem):
    """The dependency notice of one manifest edit, or the "dependency check skipped" line when it cannot run.

    Parameters: before (str or None) text before the edit; after (str) text after it; ecosystem (str) from
    `dependency_notice.ecosystem_of`.
    Returns: str, empty when there is nothing to say.
    """
    try:
        return dependency_notice.notice(before, after, ecosystem)
    except dependency_notice.DependencyCheckError as exc:
        return f"{dependency_notice.PREFIX}dependency check skipped: {exc}"


def _review_changes(changes, doc_mode, duplicate_mode, dependency_mode, comment_mode, layer_mode="off", link_mode="off",
                    decision_mode="off"):
    """The checks of an edit over (path, before, after) triples: docstrings, comments, documentation anchors, copies,
    dependency advisories, local links, layers and the decisions recorded about the touched functions, each following its mode.

    Parameters: changes (iterable of (absolute path, text before or None, text after)); the modes (`off` skips a check).
    Returns: tuple (message parts, doc findings, comment findings); the parts are empty when nothing is worth saying.
    """
    doc_findings, scope_findings, duplicate_findings, failures, dependency_parts = [], [], [], [], []
    comment_findings, layer_findings, credential_findings, caller_answers = [], [], [], []
    broken_links, unchecked_links = [], []
    decision_findings, decisions_asked = [], 0
    for path, before, after in changes:
        root = _project_of(path)
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace(os.sep, "/") if root else os.path.basename(path)
        credential_findings += [(rel, item) for item in credential_check.review(before, after)]
        if link_mode != "off":
            try:
                found = link_check.review(path, before, after)
                broken_links += [(rel, item) for item in found["broken"]]
                unchecked_links += [(rel, item) for item in found["unchecked"]]
            except (ValueError, RuntimeError, OSError) as exc:
                failures.append(f"Smart Tool links check skipped: {exc}")
        ecosystem = dependency_notice.ecosystem_of(path)
        if dependency_mode != "off" and ecosystem:
            dependency_parts += [part for part in (_dependency_part(before, after, ecosystem),) if part]
            continue
        if not doc_check.supported(path):
            continue
        edited = doc_check.written(path, before, after)
        profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile")) if root else None
        if doc_mode != "off" and root and index_profile.kind(rel, profile) == "code":
            doc_findings += [(root, rel, item) for item in doc_check.review(path, before, after, edited)]
        if comment_mode != "off" and root and index_profile.kind(rel, profile) == "code":
            comment_findings += [(rel, item) for item in comment_check.review(path, before, after, edited)]
        if doc_mode != "off" and root:
            try:
                scope_findings += doc_scope.on_edit(root, rel, before, edited["all"])
            except (OSError, UnicodeDecodeError) as exc:
                failures.append(f"Smart Tool docs scope check skipped: {exc}")
        if duplicate_mode != "off" and root and edited["touched"]:
            try:
                duplicate_findings += [(rel, item) for item in duplicates.on_write(root, path, before, after, edited)]
            except (ValueError, RuntimeError) as exc:
                failures.append(f"Smart Tool duplicates check skipped: {exc}")
        if layer_mode != "off" and root and edited["touched"]:
            try:
                layer_findings += [(rel, item) for item in layer_check.review(root, rel, before, after, edited)]
            except (ValueError, RuntimeError, OSError) as exc:
                failures.append(f"Smart Tool layers check skipped: {exc}")
        if decision_mode != "off" and root and index_profile.kind(rel, profile) == "code":
            asked = edited["touched"][:MAX_DECISION_FUNCTIONS - decisions_asked]
            decisions_asked += len(asked)
            decision_findings += _decision_findings(root, rel, asked, failures)
        if root:
            try:
                for review, key in ((removed_callers.review, "removed"), (removed_callers.signature_review, "changed")):
                    answer = review(root, rel, before, after)
                    if answer["findings"] or answer[key] and answer["note"]:
                        caller_answers.append(answer)
            except (ValueError, RuntimeError, OSError) as exc:
                failures.append(f"Smart Tool callers check skipped: {exc}")
    parts = ([_doc_lines(doc_findings)] if doc_findings else []) + \
        ([_comment_lines(comment_findings)] if comment_findings else []) + \
        ([_scope_lines(scope_findings)] if scope_findings else []) + \
        ([_duplicate_lines(duplicate_findings)] if duplicate_findings else []) + \
        ([_layer_lines(layer_findings)] if layer_findings else []) + \
        ([_decision_lines(decision_findings)] if decision_findings else []) + \
        ([_callers_lines(caller_answers)] if caller_answers else []) + \
        ([_credential_lines(credential_findings)] if credential_findings else []) + \
        _link_lines(broken_links, unchecked_links) + failures + dependency_parts
    return parts, doc_findings, comment_findings


def review_diff(changes, cfg):
    """The notes the edit hook would have added for changes made without it (a commit, a patch applied by hand).

    Parameters: changes (iterable of (absolute path, text before or None, text after)); cfg (dict) the loaded config.
    Returns: str, the hook's message for the whole set; empty when every check is quiet or off.
    """
    modes = (config.doc_mode(cfg), config.duplicate_mode(cfg), config.dependency_mode(cfg), config.comment_mode(cfg),
             config.layer_mode(cfg), config.link_mode(cfg), config.decision_mode(cfg))
    with code_graph.pending_scope():
        return " ".join(_review_changes(changes, *modes)[0])


def file_changes(root, base, rels):
    """The (path, text before, text after) triples `review_diff` takes, for the text files among `rels`.

    Parameters: root (str) folder the paths are relative to; base (str) git revision holding the text before; rels (iterable
    of str) forward-slash paths relative to root.
    Returns: list of (absolute path, text at base or None for a file new since base, current text); a file that cannot be
    read as UTF-8, is over MAX_REVIEW_BYTES or is gone is left out.
    """
    triples = []
    for rel in rels:
        full = os.path.join(root, *rel.split("/"))
        try:
            if os.path.getsize(full) > MAX_REVIEW_BYTES:
                continue
            with open(full, encoding="utf-8") as stream:
                after = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            before = affected_tests._git(root, "show", f"{base}:./{rel}")
        except ValueError:
            before = None
        triples.append((full, before, after))
    return triples


_SHELL_STARTS = {}


def note_shell_start(payload):
    """Records when a shell call (Bash, PowerShell) starts, by its tool_use_id: the PostToolUse payload carries no start
    time and no snapshot, so this is what tells the files the call wrote from the ones that were already changed."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in tool_redirect.NO_OP or not payload.get("tool_use_id"):
        return
    now = time.time()
    for old in [key for key, at in _SHELL_STARTS.items() if now - at > SHELL_START_TTL_S]:
        _SHELL_STARTS.pop(old, None)
    _SHELL_STARTS[payload["tool_use_id"]] = now


def _in_git_repository(folder):
    """Whether folder or one of its parents holds a `.git` entry (a folder, or a file for a worktree or submodule)."""
    folder = os.path.abspath(folder)
    while True:
        if os.path.exists(os.path.join(folder, ".git")):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def _shell_review(payload):
    """PostToolUse notes for the files a shell call wrote (`sed -i`, a redirection, `npm install`): the files git shows
    changed since HEAD, tracked or not, whose modification time is not older than the call's start (see
    `note_shell_start`), through the same checks as `review_diff`. A file the user or another tool changed before the
    call is left out. Anything that stops the review, a git failure included, comes back as a note.

    Parameters: payload (dict) the PostToolUse payload of the call.
    Returns: the hook output, `{}` when there is nothing to say.
    """
    started = _SHELL_STARTS.pop(payload["tool_use_id"], None)
    cwd = payload.get("cwd")
    cfg = config.load_config()
    modes = (config.doc_mode(cfg), config.duplicate_mode(cfg), config.dependency_mode(cfg), config.comment_mode(cfg),
             config.layer_mode(cfg), config.link_mode(cfg), config.decision_mode(cfg))
    if set(modes) == {"off"}:
        return {}
    if not isinstance(cwd, str) or not os.path.isdir(cwd) or not _in_git_repository(cwd):
        return {}
    if started is None:
        return _shell_note("the start of this call was not recorded, so the files it wrote cannot be told apart")
    try:
        changed = affected_tests._changes(cwd, "HEAD")
        written = sorted(rel for rel, found in changed.items() if found["status"] != "deleted"
                         and _written_since(os.path.join(cwd, *rel.split("/")), started - MTIME_SLACK_S))
        notes = review_diff(file_changes(cwd, "HEAD", written[:MAX_SHELL_REVIEW_FILES]), cfg)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return _shell_note(str(exc))
    if len(written) > MAX_SHELL_REVIEW_FILES:
        notes += f" {len(written) - MAX_SHELL_REVIEW_FILES} more changed file(s) were not reviewed."
    notes = notes.strip()
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": notes}} if notes else {}


def _written_since(path, moment):
    """Whether path was modified at or after moment (seconds since the epoch); a file that cannot be read was not."""
    try:
        return os.stat(path).st_mtime >= moment
    except OSError:
        return False


def _shell_note(reason):
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": f"Smart Tool shell review skipped: {reason}"}}


def _edit_review_scoped(payload, doc_mode, duplicate_mode, dependency_mode, comment_mode="off", layer_mode="off",
                        link_mode="off", decision_mode="off"):
    """`_edit_review` inside one `code_graph.pending_scope`, so the pending-changes scan runs once per review."""
    parts, doc_findings, comment_findings = _review_changes(
        edit_preview.preview(payload.get("tool_name"), payload.get("tool_input"), payload.get("cwd")),
        doc_mode, duplicate_mode, dependency_mode, comment_mode, layer_mode, link_mode, decision_mode)
    if not parts:
        return {}
    message = " ".join(parts)
    missing_docs = doc_mode == "require" and any(item["issue"] == "missing" for _root, _rel, item in doc_findings)
    wrong_comments = comment_mode == "require" and bool(comment_findings)
    if missing_docs or wrong_comments:
        fixes = " and ".join(text for text, needed in (("the docstring", missing_docs),
                                                      ("the comments fixed", wrong_comments)) if needed)
        return deny(message + f" Redo the edit with {fixes}.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}


def decide(payload, client, web_route, jobs):
    """Hook output for one PreToolUse payload: edits follow doc_mode (require | remind | off) and duplicate_mode
    (warn | off) and dependency_mode (warn | off) and decision_mode (warn | off), and every edit review also notes credentials the edit writes, the other tools follow hook_mode (redirect | advise | off). In redirect mode the Smart Tool call that
    replaces a redirected one starts in `jobs` under the call's tool_use_id (see `_redirect`); only a search whose
    query cannot be built, or a client that sends no tool_use_id, is denied with the exact call to make, and never in a
    subagent whose tool list has no Smart Tool MCP tool: it has nothing to switch to."""
    cfg = config.load_config()
    note_shell_start(payload)
    if isinstance(payload, dict):
        requester.note_origin(payload.get("session_id"), payload.get("cwd"))
    if isinstance(payload, dict) and payload.get("tool_name") in EDIT_TOOLS:
        doc_mode, duplicate_mode, dependency_mode, comment_mode, layer_mode, link_mode, decision_mode = (
            config.doc_mode(cfg), config.duplicate_mode(cfg), config.dependency_mode(cfg), config.comment_mode(cfg),
            config.layer_mode(cfg), config.link_mode(cfg), config.decision_mode(cfg))
        if "off" == doc_mode == duplicate_mode == dependency_mode == comment_mode == layer_mode == link_mode == decision_mode:
            return {}
        return _edit_review(payload, doc_mode, duplicate_mode, dependency_mode, comment_mode, layer_mode, link_mode,
                            decision_mode)
    if isinstance(payload, dict) and str(payload.get("tool_name")).startswith(f"mcp__{SMART_TOOL_SERVER}__"):
        if payload["tool_name"] == SMART_SEARCH_TOOL:
            router.CLIENT.set(client)
            router.SESSION.set(payload.get("session_id"))
            router._log_decision(SMART_SEARCH_TOOL, {}, "allow", "Smart Tool search, logged to measure followed redirects")
        requester.note_hook(payload["tool_name"].removeprefix(f"mcp__{SMART_TOOL_SERVER}__"), payload.get("tool_input"),
                            payload.get("session_id"), payload.get("cwd"))
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
    return _answer_in_place(payload, client, plan, jobs)


def _start_job(payload, client, plan, jobs, key):
    """Starts the Smart Tool call of a redirected one under key (web calls carry the requester of the hook)."""
    who = requester.CURRENT.set(requester.from_hook(client, payload)) if payload["tool_name"] in WEB_TOOLS else None
    try:
        jobs.start(key, plan["kind"], plan["arguments"])
    finally:
        if who is not None:
            requester.CURRENT.reset(who)


def _codex_print(payload, client, jobs, key, text):
    """Codex shell call rewritten to print the answer; `allow` is what Codex needs to apply the rewrite."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    jobs.forget(key)
    _log_served(client, payload)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "Smart Tool answered this search",
                                   "updatedInput": {**tool_input, "command": tool_redirect.codex_command(text)}}}


def _answer_in_place(payload, client, plan, jobs):
    """A redirected call that cannot be swapped by `tool_use_id` (no id, or no query to run) is still not denied: the
    Smart Tool answer goes into the context of the call, which runs as written, and a codex shell search prints it.
    Without an answer in time, or without a query, the context names the exact Smart Tool call to make."""
    if not agent_has_smart_tool(payload.get("agent_type") if payload.get("agent_id") else None, payload.get("cwd")):
        return {}
    if not plan["kind"]:
        return advise(plan["message"])
    key = f"noid-{uuid.uuid4().hex}"
    _start_job(payload, client, plan, jobs, key)
    text = _answer(jobs, key, PRE_WAIT_S, payload["tool_name"])
    if text is None:
        jobs.forget(key)
        return advise(plan["message"])
    if client == "codex" and payload["tool_name"] in tool_redirect.NO_OP:
        return _codex_print(payload, client, jobs, key, text)
    jobs.forget(key)
    _log_served(client, payload)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": f"Smart Tool answered this {payload['tool_name']} call with "
                                                        f"smart_search (the call was not blocked):\n{text}"}}


def _redirect(payload, client, plan, jobs):
    """Starts the Smart Tool call of a redirected one. A shell search whose answer comes within PRE_WAIT_S is rewritten
    so the answer is all it prints: a no-op whose output the PostToolUse replaces in Claude Code, a `cat` of a file
    holding the answer in Codex (which has no output replacement and applies a rewrite only with `allow`). Anything
    else runs as the agent wrote it and, in Claude Code, `after` swaps its output for the answer."""
    key, tool_name = payload["tool_use_id"], payload["tool_name"]
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    _start_job(payload, client, plan, jobs, key)
    if tool_name not in tool_redirect.NO_OP:
        return {}
    text = _answer(jobs, key, PRE_WAIT_S, tool_name)
    if text is None:
        return {}
    if client != "codex":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "updatedInput": {**tool_input, "command": tool_redirect.NO_OP[tool_name]}}}
    return _codex_print(payload, client, jobs, key, text)


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
    the model reads (`updatedToolOutput`); for a shell call with no such answer, the notes of `_shell_review` on the files
    it wrote; else `{}` (the native output stays)."""
    if not isinstance(payload, dict) or not payload.get("tool_use_id"):
        return {}
    key, tool_name = payload["tool_use_id"], payload.get("tool_name")
    text = _answer(jobs, key, WEB_POST_WAIT_S if tool_name in WEB_TOOLS else SEARCH_POST_WAIT_S, tool_name)
    jobs.forget(key)
    output = tool_redirect.output_for(tool_name, payload.get("tool_response"), text) if text else None
    if output is None:
        return _shell_review(payload) if tool_name in tool_redirect.NO_OP else {}
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
    if tool_name in tool_redirect.NO_OP and tool_input.get("run_in_background"):
        return {}
    if decision in ("instruct", "redirect") and not _indexable(target, cwd, payload.get("session_id")):
        return {}
    if decision == "instruct":
        if not agent_has_smart_tool(payload.get("agent_type") if payload.get("agent_id") else None, cwd):
            return {}
        return advise(instruct_message(client, tool_name, target, reason, cwd))
    if decision != "redirect":
        return {}
    terms = redirect_rule.search_terms(tool_name, tool_input)
    arguments = {"project_root": _search_root(target, cwd), "query_identifiers": terms,
                 "single_language_reason": SINGLE_LANGUAGE_REASON}
    return {"kind": "smart_search" if terms else None, "arguments": arguments,
            "message": redirect_message(client, tool_name, target, reason, cwd)}
