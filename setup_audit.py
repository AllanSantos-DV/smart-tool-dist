#!/usr/bin/env python3
"""Setup audit: what Claude Code and Codex actually load for a project, and where the pieces collide.

Collects the effective set of both harnesses (settings layers, enabled plugins, hooks, MCP servers, skills and the
instruction chain) and runs deterministic checks, each tied to the documented behavior it relies on. Nothing is
edited. What could not be checked is reported as not verified, never as ok. MCP servers are started (stdio) or
called (HTTP) the way a session would, with `initialize` and `tools/list`, unless probe=False. Secrets stay out of
the report: env and header values are never read into it, only their names.

Usage: python setup_audit.py [--project DIR] [--no-probe] [--json]
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

import yaml

import atomic_io
import endpoint_sync
import paths

REPORT_DIR = os.path.join(paths.DATA_DIR, "setup-audit")
INDEX_NAME = "index.json"
PROBE_TIMEOUT_S = 15
SCHEDULE_S = 30 * 60
FIRST_RUN_DELAY_S = 120
PROBE_MAX_AGE_S = 6 * 3600
SKILL_DESCRIPTION_MAX = 1536
SKILL_BUDGET_CHARS = 8000
CODEX_PROJECT_DOC_MAX = 32 * 1024
CLAUDE_TOOLS = ("Bash", "Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch",
                "WebSearch", "Task")
CODEX_TOOLS = ("Bash", "apply_patch")
DECIDING_EVENTS = ("PreToolUse",)
DOCS = {"claude_hooks": "https://code.claude.com/docs/en/hooks.md", "codex_hooks": "https://learn.chatgpt.com/docs/hooks.md",
        "claude_skills": "https://code.claude.com/docs/en/skills.md",
        "codex_skills": "https://learn.chatgpt.com/docs/build-skills.md",
        "claude_memory": "https://code.claude.com/docs/en/memory.md",
        "codex_agents_md": "https://developers.openai.com/codex/guides/agents-md",
        "claude_mcp": "https://code.claude.com/docs/en/mcp.md", "mcp_lifecycle":
        "https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle"}
_SCRIPT_RE = re.compile(r"""["']?([^\s"'|&;<>]+\.(?:js|cjs|mjs|py|ps1|sh|bat|cmd|exe))["']?""", re.IGNORECASE)
_SILENCED_RE = re.compile(r"\|\|\s*(?:true|exit\s+0)\b|2>\s*(?:/dev/null|nul)\b|;\s*exit\s+0\s*$", re.IGNORECASE)
_IMPORT_RE = re.compile(r"(?<![\w`])@((?:~|\.{1,2})?/?[\w.\-/\\]+\.\w+)")


def _home(*parts):
    """Path under the user's home folder."""
    return os.path.join(os.path.expanduser("~"), *parts)


def codex_dir():
    """Codex's home: CODEX_HOME when set, else ~/.codex."""
    return os.environ.get("CODEX_HOME") or _home(".codex")


def _managed_settings():
    """Where an administrator's managed-settings.json can live on this OS (highest settings layer)."""
    roots = (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramData", r"C:\ProgramData"))
    return [os.path.join(root, "ClaudeCode", "managed-settings.json") for root in roots] if os.name == "nt" else \
        ["/Library/Application Support/ClaudeCode/managed-settings.json", "/etc/claude-code/managed-settings.json"]


class _Snapshot:
    """Files read while collecting (path -> sha256) plus what could not be read."""

    def __init__(self):
        self.files, self.unverified = {}, []

    def read(self, path):
        try:
            with open(path, "rb") as stream:
                data = stream.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self.unverified.append(f"{path}: {exc}")
            return None
        self.files[os.path.abspath(path)] = hashlib.sha256(data).hexdigest()
        return data.decode("utf-8-sig", "replace")

    def json(self, path):
        text = self.read(path)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            self.unverified.append(f"{path}: invalid JSON ({exc})")
            return None
        return data if isinstance(data, dict) else None

    def toml(self, path):
        text = self.read(path)
        if text is None:
            return None
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self.unverified.append(f"{path}: invalid TOML ({exc})")
            return None


def _hooks_from(data, source, root=None):
    """Hook entries of a settings/hooks.json object: one dict per hook with its event, matcher and target."""
    found = []
    for event, groups in ((data or {}).get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(hook, dict):
                    found.append({"event": event, "matcher": group.get("matcher") or "", "type": hook.get("type"),
                                  "command": hook.get("command"), "url": hook.get("url"),
                                  "server": hook.get("server"), "tool": hook.get("tool"), "source": source,
                                  "root": root})
    return found


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_vars(value, extra):
    """${VAR} and ${VAR:-default} as Claude Code expands them in .mcp.json (extra: CLAUDE_PLUGIN_ROOT and such)."""
    if not isinstance(value, str):
        return value
    return _VAR_RE.sub(lambda m: extra.get(m.group(1)) or os.environ.get(m.group(1)) or m.group(2) or m.group(0), value)


def _mcp_entries(servers, scope, source, prefix="", extra=None):
    """MCP server entries of one config (enabled only); env and header values kept in private fields. prefix names
    a plugin's servers the way Claude Code does (plugin:<plugin>:<server>)."""
    entries, extra = [], extra or {}
    for name, spec in (servers or {}).items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        spec = {k: ([_expand_vars(a, extra) for a in v] if isinstance(v, list) else
                    {kk: _expand_vars(vv, extra) for kk, vv in v.items()} if isinstance(v, dict) else
                    _expand_vars(v, extra)) for k, v in spec.items()}
        entries.append({"name": prefix + name, "scope": scope, "source": source, "url": spec.get("url"),
                        "transport": "http" if spec.get("url") else "stdio", "command": spec.get("command"),
                        "args": [str(a) for a in spec.get("args") or []],
                        "env_names": sorted((spec.get("env") or {}).keys()),
                        "_env": spec.get("env") or {}, "_headers": dict(spec.get("headers") or spec.get("http_headers")
                                                                       or {}),
                        "_bearer_env": spec.get("bearer_token_env_var"),
                        "disabled_tools": list(spec.get("disabled_tools") or [])})
    return entries


def _frontmatter(text):
    """YAML frontmatter of a SKILL.md as a dict ({} when absent or invalid)."""
    match = re.match(r"^---\s*\n(.*?)\n---", text or "", re.S)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _skills(snapshot, folder, source, recursive=False, disabled=()):
    """Skills (name, description, path) under folder, skipping names turned off in skillOverrides."""
    found = []
    pattern = "**/SKILL.md" if recursive else "*/SKILL.md"
    from pathlib import Path
    for path in sorted(Path(folder).glob(pattern)) if os.path.isdir(folder) else []:
        if path.parent.name in disabled:
            continue
        meta = _frontmatter(snapshot.read(str(path)))
        found.append({"name": str(meta.get("name") or path.parent.name), "path": str(path), "source": source,
                      "description": " ".join(str(meta.get("description") or "").split())})
    return found


def _ancestors(project):
    """Folders from the drive root down to project, in that order."""
    chain, current = [], os.path.abspath(project)
    while True:
        chain.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return list(reversed(chain))
        current = parent


def _approvals(data, approval):
    """Folds one settings object's .mcp.json approvals (enableAllProjectMcpServers, enabledMcpjsonServers,
    disabledMcpjsonServers) into approval."""
    approval["all"] = approval["all"] or data.get("enableAllProjectMcpServers") is True
    approval["on"].update(data.get("enabledMcpjsonServers") or [])
    approval["off"].update(data.get("disabledMcpjsonServers") or [])


def collect_claude(project, snapshot):
    """Claude Code's effective set for project: settings layers, enabled plugins, hooks, MCP, skills, CLAUDE.md."""
    layers = [(path, "managed") for path in _managed_settings()] + [
        (os.path.join(endpoint_sync.claude_dir(), "settings.json"), "user"),
        (os.path.join(project, ".claude", "settings.json"), "project"),
        (os.path.join(project, ".claude", "settings.local.json"), "local")]
    hooks, enabled, overrides, settings, approval = [], {}, {}, [], {"all": False, "on": set(), "off": set()}
    for path, scope in layers:
        data = snapshot.json(path)
        if data is None:
            continue
        settings.append({"scope": scope, "path": path, "disable_all_hooks": bool(data.get("disableAllHooks"))})
        hooks += _hooks_from(data, f"settings:{scope}")
        enabled.update({k: v for k, v in (data.get("enabledPlugins") or {}).items()})
        overrides.update(data.get("skillOverrides") or {})
        _approvals(data, approval)
    disabled_skills = {name for name, value in overrides.items() if value == "off"}
    state = snapshot.json(endpoint_sync.claude_json()) or {}
    mcp = _mcp_entries(state.get("mcpServers"), "user", endpoint_sync.claude_json())
    project_state = (state.get("projects") or {}).get(os.path.abspath(project).replace("\\", "/")) or \
        (state.get("projects") or {}).get(os.path.abspath(project)) or {}
    mcp += _mcp_entries(project_state.get("mcpServers"), "local", endpoint_sync.claude_json())
    _approvals(project_state, approval)
    project_mcp = snapshot.json(os.path.join(project, ".mcp.json")) or {}
    shared = _mcp_entries(project_mcp.get("mcpServers"), "project", os.path.join(project, ".mcp.json"))
    unapproved = [e["name"] for e in shared
                  if e["name"] in approval["off"] or not (approval["all"] or e["name"] in approval["on"])]
    mcp += [e for e in shared if e["name"] not in unapproved]
    skills = _skills(snapshot, os.path.join(endpoint_sync.claude_dir(), "skills"), "user", disabled=disabled_skills)
    skills += _skills(snapshot, os.path.join(project, ".claude", "skills"), "project", disabled=disabled_skills)
    plugins = []
    installed = snapshot.json(os.path.join(endpoint_sync.claude_dir(), "plugins", "installed_plugins.json")) or {}
    for plugin_id, installs in (installed.get("plugins") or {}).items():
        if not enabled.get(plugin_id):
            continue
        for install in installs if isinstance(installs, list) else []:
            root = install.get("installPath")
            if install.get("projectPath") and os.path.abspath(install["projectPath"]) != os.path.abspath(project):
                continue
            if not root or not os.path.isdir(root):
                snapshot.unverified.append(f"plugin {plugin_id}: install path missing ({root})")
                continue
            name = plugin_id.split("@")[0]
            plugins.append({"id": plugin_id, "path": root})
            hooks += _hooks_from(snapshot.json(os.path.join(root, "hooks", "hooks.json")), f"plugin:{name}", root)
            plugin_mcp = snapshot.json(os.path.join(root, ".mcp.json")) or {}
            mcp += _mcp_entries(plugin_mcp.get("mcpServers", plugin_mcp), f"plugin:{name}",
                                os.path.join(root, ".mcp.json"), f"plugin:{name}:", {"CLAUDE_PLUGIN_ROOT": root})
            skills += [{**s, "name": f"{name}:{s['name']}"} for s in
                       _skills(snapshot, os.path.join(root, "skills"), f"plugin:{name}")]
    instructions = []
    for path, kind in [(os.path.join(endpoint_sync.claude_dir(), "CLAUDE.md"), "user")] + [
            (os.path.join(folder, name), "project") for folder in _ancestors(project)
            for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md"))]:
        text = snapshot.read(path)
        if text is not None:
            instructions.append({"path": path, "kind": kind, "bytes": len(text.encode("utf-8")), "_text": text})
    return {"settings": settings, "plugins": plugins, "hooks": hooks, "mcp": mcp, "skills": skills,
            "instructions": instructions, "unapproved_mcp": unapproved}


def collect_codex(project, snapshot):
    """Codex's effective set for project: config.toml (MCP, hooks), hooks.json, skills, AGENTS.md chain."""
    config = snapshot.toml(os.path.join(codex_dir(), "config.toml")) or {}
    mcp = _mcp_entries(config.get("mcp_servers"), "user", os.path.join(codex_dir(), "config.toml"))
    hooks = _hooks_from(snapshot.json(os.path.join(codex_dir(), "hooks.json")), "hooks.json")
    hooks += _hooks_from(config, "config.toml")
    skills = _skills(snapshot, os.path.join(codex_dir(), "skills"), "user", recursive=True)
    skills += _skills(snapshot, _home(".agents", "skills"), "user-agents")
    skills += _skills(snapshot, os.path.join(project, ".agents", "skills"), "project")
    instructions = []
    for name in ("AGENTS.override.md", "AGENTS.md"):
        text = snapshot.read(os.path.join(codex_dir(), name))
        if text is not None:
            instructions.append({"path": os.path.join(codex_dir(), name), "kind": "user",
                                 "bytes": len(text.encode("utf-8")), "_text": text})
            break
    git_root = next((folder for folder in reversed(_ancestors(project)) if os.path.exists(os.path.join(folder, ".git"))),
                    os.path.abspath(project))
    for folder in _ancestors(project):
        if len(folder) < len(git_root):
            continue
        for name in ("AGENTS.override.md", "AGENTS.md"):
            text = snapshot.read(os.path.join(folder, name))
            if text is not None:
                instructions.append({"path": os.path.join(folder, name), "kind": "project",
                                     "bytes": len(text.encode("utf-8")), "_text": text})
                break
    return {"hooks": hooks, "mcp": mcp, "skills": skills, "instructions": instructions,
            "plugins_unverified": bool(config.get("plugins"))}


def _finding(check, severity, harness, subject, detail, source, evidence=()):
    """One finding; its id is stable for the same check, harness and subject (used to diff runs)."""
    key = hashlib.sha1(f"{check}\0{harness}\0{subject}".encode()).hexdigest()[:10]
    return {"id": key, "check": check, "severity": severity, "harness": harness, "subject": subject,
            "detail": detail, "source": source, "evidence": list(evidence)}


def _matches(matcher, tool):
    """Whether a hook matcher (empty or * for all, else a regex over the tool name) covers tool."""
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def check_hook_overlap(harness, hooks, tools):
    """Deciding hooks (PreToolUse) that run on the same tool: they run in parallel; a deny wins, and two rewrites
    (updatedInput replaces the whole input) have no documented order."""
    findings, groups = [], {}
    for event in DECIDING_EVENTS:
        for tool in tools:
            matching = [h for h in hooks if h["event"] == event and _matches(h["matcher"], tool)]
            if len(matching) > 1:
                key = (event, tuple(sorted(f"{h['source']} {h['type']} {h['matcher'] or '*'}" for h in matching)))
                groups.setdefault(key, []).append(tool)
    doc = DOCS["claude_hooks"] if harness == "claude" else DOCS["codex_hooks"]
    rule = ("all run in parallel; deny > defer > ask > allow, and two updatedInput rewrites have no documented order"
            if harness == "claude" else "they run concurrently and any deny wins")
    for (event, members), names in groups.items():
        sources = sorted({m.split(" ")[0] for m in members})
        findings.append(_finding("hook_overlap", "warn", harness, f"{event} on {', '.join(names)}",
                                 f"{len(members)} hooks decide on the same call ({rule}): " + "; ".join(members)
                                 + (". From different sources: check they do not rewrite the same input."
                                    if len(sources) > 1 else "."), doc))
    return findings


def _expand(token, root):
    """A hook script path with ${CLAUDE_PLUGIN_ROOT}, ~ and environment variables expanded."""
    token = token.replace("${CLAUDE_PLUGIN_ROOT}", root or "${CLAUDE_PLUGIN_ROOT}")
    return os.path.expandvars(os.path.expanduser(token))


def check_hook_targets(harness, hooks, mcp_names, probe):
    """Hooks that cannot work as configured: missing script, silenced errors, unknown MCP server, endpoint down."""
    findings = []
    doc = DOCS["claude_hooks"] if harness == "claude" else DOCS["codex_hooks"]
    for hook in hooks:
        where = f"{hook['source']} {hook['event']} {hook['matcher'] or '*'}"
        if hook["type"] == "command" and hook["command"]:
            for token in _SCRIPT_RE.findall(hook["command"]):
                path = _expand(token, hook.get("root"))
                if "$" in path or "%" in path:
                    continue
                if not os.path.isabs(path):
                    path = os.path.join(hook["root"], path) if hook.get("root") else None
                if path and not os.path.exists(path):
                    findings.append(_finding("hook_missing_script", "error", harness, where,
                                             f"The hook runs {token}, which does not exist.", doc, [hook["command"]]))
            if _SILENCED_RE.search(hook["command"]):
                findings.append(_finding("hook_silenced", "warn", harness, where,
                                         "The hook discards its errors (|| true / 2>/dev/null): when it breaks, "
                                         "nothing shows.", doc, [hook["command"]]))
        elif hook["type"] == "mcp_tool" and hook.get("server") and hook["server"] not in mcp_names:
            findings.append(_finding("hook_unknown_server", "error", harness, where,
                                     f"The hook calls MCP server '{hook['server']}', which is not configured here.",
                                     doc))
        elif hook["type"] == "http" and hook.get("url") and probe:
            problem = _reachable(hook["url"])
            if problem:
                findings.append(_finding("hook_endpoint_down", "error", harness, where,
                                         f"The hook posts to {hook['url']}, which does not answer: {problem}", doc))
    return findings


def _reachable(url):
    """None when a TCP connection to the URL's host and port opens, else the error."""
    parsed = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)),
                                      timeout=3):
            return None
    except OSError as exc:
        return str(exc)


def check_skills(harness, skills):
    """Skill name collisions, descriptions over Claude's per-skill cut, and the total over the list budget."""
    findings, by_name = [], {}
    doc = DOCS["claude_skills"] if harness == "claude" else DOCS["codex_skills"]
    for skill in skills:
        by_name.setdefault(skill["name"], []).append(skill)
    for name, same in by_name.items():
        if len(same) > 1:
            findings.append(_finding("skill_name_collision", "warn", harness, name,
                                     f"{len(same)} skills share the name '{name}': only one is offered.", doc,
                                     [s["path"] for s in same]))
    if harness == "claude":
        for skill in skills:
            if len(skill["description"]) > SKILL_DESCRIPTION_MAX:
                findings.append(_finding("skill_description_long", "warn", harness, skill["name"],
                                         f"Description of {len(skill['description'])} characters; Claude Code cuts "
                                         f"each one at {SKILL_DESCRIPTION_MAX}.", doc, [skill["path"]]))
    total = sum(len(s["name"]) + len(s["description"]) for s in skills)
    if total > SKILL_BUDGET_CHARS:
        findings.append(_finding("skill_budget", "warn", harness, f"{len(skills)} skills",
                                 f"Names and descriptions add up to {total:,} characters, over the ~{SKILL_BUDGET_CHARS:,} "
                                 "the skill list gets (Claude: 1% of a 200k context; Codex: 2% or 8,000 characters):"
                                 " some skills are left out of what the model sees.", doc))
    return findings


def check_instructions(project, claude, codex):
    """AGENTS.md that only Codex reads, broken @imports in CLAUDE.md, and a Codex chain over its byte limit."""
    findings = []
    claude_texts = [i for i in claude["instructions"]]
    agents_here = os.path.exists(os.path.join(project, "AGENTS.md"))
    imports_agents = any(re.search(r"(?<![\w`])@\S*AGENTS\.md", i["_text"]) for i in claude_texts)
    if agents_here and not imports_agents:
        findings.append(_finding("agents_md_not_read_by_claude", "warn", "claude", os.path.join(project, "AGENTS.md"),
                                 "Codex reads AGENTS.md; Claude Code reads only CLAUDE.md (and what it imports with "
                                 "@AGENTS.md), so the two agents get different instructions here.", DOCS["claude_memory"]))
    for item in claude_texts:
        base = os.path.dirname(item["path"])
        for target in _IMPORT_RE.findall(item["_text"]):
            path = os.path.expanduser(target) if target.startswith("~") else os.path.join(base, target)
            if not os.path.exists(path):
                findings.append(_finding("broken_import", "error", "claude", f"{item['path']} @{target}",
                                         "Imported file does not exist: that part of the instructions never loads.",
                                         DOCS["claude_memory"], [item["path"]]))
    total = sum(i["bytes"] for i in codex["instructions"] if i["kind"] == "project")
    if total > CODEX_PROJECT_DOC_MAX:
        findings.append(_finding("codex_agents_truncated", "error", "codex", project,
                                 f"Project AGENTS.md chain is {total:,} bytes; Codex reads {CODEX_PROJECT_DOC_MAX:,} "
                                 "(project_doc_max_bytes) and drops the rest.", DOCS["codex_agents_md"],
                                 [i["path"] for i in codex["instructions"]]))
    return findings


def _rpc_stdio(entry, cwd, timeout):
    """initialize + tools/list over stdio; {"tools": [...]} or {"error", "kind": missing|down|timeout}."""
    command = entry["command"]
    resolved = command if os.path.isabs(command or "") else shutil.which(command or "")
    if not resolved or not os.path.exists(resolved):
        return {"error": f"command not found: {command}", "kind": "missing"}
    env = {**os.environ, **{k: str(v) for k, v in entry["_env"].items()}}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen([resolved, *entry["args"]], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", creationflags=flags)
    except OSError as exc:
        return {"error": f"did not start: {exc}", "kind": "down"}
    lines, errors = queue.Queue(), []
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout] + [lines.put(None)], daemon=True).start()
    threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
    deadline = time.monotonic() + timeout

    def call(request_id, method, params):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"no answer to {method} in {timeout}s") from None
            if line is None:
                raise RuntimeError(f"exited: {''.join(errors).strip()[-200:] or 'no output'}")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message"))[:200])
                return message["result"]
    try:
        call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "smart-tool-setup-audit", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        return {"tools": call(2, "tools/list", {}).get("tools") or []}
    except (OSError, RuntimeError, TimeoutError) as exc:
        return {"error": str(exc), "kind": "timeout" if isinstance(exc, TimeoutError) else "down"}
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=flags)
        else:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _rpc_http(entry, timeout):
    """initialize + tools/list over streamable HTTP; {"tools": [...]} or {"error", "kind": auth|down}."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               **{k: str(v) for k, v in entry["_headers"].items()}}
    if entry.get("_bearer_env") and os.environ.get(entry["_bearer_env"]):
        headers["Authorization"] = f"Bearer {os.environ[entry['_bearer_env']]}"

    def post(body):
        request = urllib.request.Request(entry["url"], data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            text = response.read().decode("utf-8", "replace")
        if not text.strip():
            return {}
        if text.lstrip().startswith("{"):
            return json.loads(text)
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1]) if data else {}
    try:
        post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "smart-tool-setup-audit", "version": "1"}}})
        post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        answer = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return {"tools": (answer.get("result") or {}).get("tools") or []}
    except urllib.error.HTTPError as exc:
        kind = "auth" if exc.code in (401, 403) else "down"
        return {"error": f"HTTP {exc.code}", "kind": kind}
    except (OSError, ValueError) as exc:
        return {"error": str(getattr(exc, "reason", exc)), "kind": "down"}


_PROBES = {}
_PROBES_LOCK = threading.Lock()


def _probe_key(entry, cwd):
    """Identity of a server launch (secrets only as a hash), shared by the projects and harnesses that register it;
    the folder counts for servers a project registers itself and for arguments that may be relative paths."""
    relative = any(not a.startswith("-") and not os.path.isabs(a) and ("/" in a or "\\" in a or "." in a)
                   for a in entry["args"])
    per_project = entry["transport"] == "stdio" and (entry["scope"] in ("project", "local") or relative)
    spec = [entry["transport"], entry.get("command"), entry["args"], entry.get("url"), sorted(entry["_env"].items()),
            sorted(entry["_headers"].items()), entry.get("_bearer_env"), cwd if per_project else None]
    return hashlib.sha256(json.dumps(spec, default=str).encode()).hexdigest()


def probe_mcp(entries, cwd, timeout=PROBE_TIMEOUT_S, max_age=0):
    """Tools of each MCP server, started or called as a session would (in parallel). With max_age, a result of the
    same launch younger than that is reused (failures never are: a server fixed meanwhile shows at once): the periodic
    audit does not start every server of every project each time. Each result carries `probed_at`."""
    def one(entry):
        key = _probe_key(entry, cwd)
        with _PROBES_LOCK:
            cached = _PROBES.get(key)
        if max_age and cached and not cached.get("error") and time.time() - cached["probed_at"] < max_age:
            return cached
        # A server whose config cannot even be used (a URL with an unexpanded variable, a bad port) is a finding.
        try:
            result = _rpc_http(entry, timeout) if entry["transport"] == "http" else _rpc_stdio(entry, cwd, timeout)
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"[:200], "kind": "down"}
        result["probed_at"] = time.time()
        with _PROBES_LOCK:
            _PROBES[key] = result
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, entries))


def check_mcp(harness, entries, results):
    """Unhealthy servers, and tools offered twice (same name, or a proxy re-exposing a directly registered server)."""
    findings, tools_by_server = [], {}
    doc = DOCS["claude_mcp"] if harness == "claude" else DOCS["mcp_lifecycle"]
    for entry, result in zip(entries, results):
        subject = f"{entry['name']} ({entry['scope']})"
        if result is None:
            continue
        if result.get("error"):
            severity = {"missing": "error", "down": "error", "timeout": "warn", "auth": "info"}[result["kind"]]
            text = {"auth": "needs sign-in; its tools were not verified",
                    "timeout": "did not answer in time; its tools were not verified"}.get(result["kind"],
                                                                                       "does not start or answer")
            findings.append(_finding("mcp_unhealthy", severity, harness, subject,
                                     f"MCP server {text}: {result['error']}", doc, [entry["source"]]))
            continue
        names = [t.get("name") for t in result["tools"] if t.get("name") not in entry["disabled_tools"]]
        tools_by_server[entry["name"]] = set(names)
        chars = sum(len(json.dumps(t)) for t in result["tools"])
        entry["tools"], entry["tool_chars"] = len(names), chars
    for server, names in tools_by_server.items():
        for other, other_names in tools_by_server.items():
            if other == server:
                continue
            same = sorted(n for n in names if n in other_names) if server < other else []
            proxied = sorted(n for n in names if "__" in n and n.split("__", 1)[0] == other
                             and n.split("__", 1)[1] in other_names)
            if same:
                findings.append(_finding("mcp_duplicate_tools", "warn", harness, f"{server} and {other}",
                                         f"{len(same)} tools with the same name on both servers: {', '.join(same[:6])}",
                                         doc))
            if proxied:
                findings.append(_finding("mcp_duplicate_tools", "warn", harness, f"{server} proxies {other}",
                                         f"{server} re-exposes {len(proxied)} tools of {other}, which is also "
                                         f"registered directly: {', '.join(proxied[:6])}. The model sees both and "
                                         "pays for both descriptions.", doc))
    return findings


def _public(section):
    """A collected section without private fields (instruction text, env and header values)."""
    out = {}
    for key, value in section.items():
        if isinstance(value, list):
            out[key] = [{k: v for k, v in item.items() if not k.startswith("_")} if isinstance(item, dict) else item
                        for item in value]
        else:
            out[key] = value
    return out


def run(project, probe=True, save=True, probe_max_age=0, trigger="manual"):
    """Collects both harnesses for project, runs every check and returns the report; with save, stores it and
    marks findings new or resolved against the previous report of the same project. probe_max_age reuses recent
    MCP probes (periodic runs); trigger records who asked (manual or schedule)."""
    project = os.path.abspath(project)
    snapshot = _Snapshot()
    claude, codex = collect_claude(project, snapshot), collect_codex(project, snapshot)
    findings = []
    for harness, data, tools in (("claude", claude, CLAUDE_TOOLS), ("codex", codex, CODEX_TOOLS)):
        names = {m["name"] for m in data["mcp"]}
        findings += check_hook_overlap(harness, data["hooks"], tools)
        findings += check_hook_targets(harness, data["hooks"], names, probe)
        findings += check_skills(harness, data["skills"])
        results = (probe_mcp(data["mcp"], project, max_age=probe_max_age) if probe
                   else [None] * len(data["mcp"]))
        findings += check_mcp(harness, data["mcp"], results)
    findings += check_instructions(project, claude, codex)
    unverified = list(snapshot.unverified)
    if not probe:
        unverified.append("MCP servers and HTTP hook endpoints were not contacted (probe off)")
    for name in claude["unapproved_mcp"]:
        unverified.append(f".mcp.json server '{name}' is not approved in Claude Code, so it is neither loaded nor "
                          "started here")
    if codex.get("plugins_unverified"):
        unverified.append("Codex plugins (their skills and hooks) are not collected yet")
    unverified += ["skill trigger overlap and contradictions between instructions need a model (not run)",
                   "the order between two parallel rewrites is undocumented and cannot be checked"]
    key = hashlib.sha256(json.dumps(sorted(snapshot.files.items())).encode()).hexdigest()[:16]
    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["harness"], f["check"], f["subject"]))
    report = {"project": project, "checked_at": time.time(), "snapshot_key": key, "probe": probe, "trigger": trigger,
              "summary": {s: sum(f["severity"] == s for f in findings) for s in order},
              "findings": findings, "unverified": unverified,
              "claude": _public(claude), "codex": _public(codex), "files": len(snapshot.files)}
    if save:
        _save_with_diff(report)
    return report


def _report_path(project):
    return os.path.join(REPORT_DIR, hashlib.sha1(os.path.abspath(project).lower().encode()).hexdigest()[:12] + ".json")


def last_report(project):
    """The last stored report of project, or None when it was never audited."""
    try:
        with open(_report_path(project), encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


def stored_for(path):
    """The last stored report of path or of its nearest audited ancestor folder, or None when none was audited."""
    folder = os.path.abspath(path)
    while True:
        report = last_report(folder)
        if report is not None:
            return report
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def session_notice(path):
    """One short line for the agent about the stored audit of path (nothing is run), or "" when there is no stored
    report or it holds no error or warning."""
    report = stored_for(path)
    if report is None:
        return ""
    summary = report["summary"]
    if not summary["error"] and not summary["warn"]:
        return ""
    new = sum(1 for f in report["findings"] if f.get("new") and f["severity"] in ("error", "warn"))
    age = max(0, int((time.time() - report["checked_at"]) // 60))
    return (f"Setup audit of {report['project']} ({age} min ago): {summary['error']} errors, {summary['warn']} "
            f"warnings" + (f", {new} new" if new else "") + " in the agent configuration. Details without rerunning: "
            "project_manage action=setup_audit mode=last.")


def index():
    """{project: {summary, new, resolved, checked_at, trigger}} of every audited project (the periodic summary)."""
    try:
        with open(os.path.join(REPORT_DIR, INDEX_NAME), encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.join(REPORT_DIR, INDEX_NAME)} is not a setup audit index: fix or delete it.")
    return data


_INDEX_LOCK = threading.Lock()


def index_key(project):
    """The index key of a folder: the same for any spelling of it on this OS."""
    return os.path.normcase(os.path.abspath(project))


def _write_index(data):
    atomic_io.write_secret_text(os.path.join(REPORT_DIR, INDEX_NAME), json.dumps(data, indent=1, ensure_ascii=False))


def prune_index(projects):
    """Drops the index entries and reports of folders no longer registered."""
    keep = {index_key(p) for p in projects}
    with _INDEX_LOCK:
        data = index()
        gone = [key for key in data if key not in keep]
        for key in gone:
            data.pop(key)
            try:
                os.remove(_report_path(key))
            except FileNotFoundError:
                pass
        if gone:
            _write_index(data)
    return gone


def _save_with_diff(report):
    """Stores the report, marks findings new or resolved against the previous one and updates the index, all under
    one lock: the scheduler, Run now and project_manage may audit the same folder at once."""
    with _INDEX_LOCK:
        _save_locked(report)


def _save_locked(report):
    os.makedirs(REPORT_DIR, exist_ok=True)
    last_path = _report_path(report["project"])
    try:
        with open(last_path, encoding="utf-8") as stream:
            previous = json.load(stream)
    except (FileNotFoundError, ValueError):
        previous = None
    if previous:
        before = {f["id"] for f in previous.get("findings", [])}
        now = {f["id"] for f in report["findings"]}
        report["changes"] = {"snapshot_changed": previous.get("snapshot_key") != report["snapshot_key"],
                             "new": sorted(now - before), "resolved": [f for f in previous["findings"]
                                                                       if f["id"] not in now]}
        for finding in report["findings"]:
            finding["new"] = finding["id"] not in before
    atomic_io.write_secret_text(last_path, json.dumps(report, indent=1, ensure_ascii=False))
    changes = report.get("changes") or {}
    data = index()
    data[index_key(report["project"])] = {"summary": report["summary"], "new": len(changes.get("new", [])),
                                          "resolved": len(changes.get("resolved", [])),
                                          "checked_at": report["checked_at"], "trigger": report["trigger"],
                                          "snapshot_key": report["snapshot_key"]}
    _write_index(data)


class Scheduler:
    """Periodic mode: audits every registered project whose folder exists, FIRST_RUN_DELAY_S after start and then
    every SCHEDULE_S, one project at a time, reusing MCP probes younger than PROBE_MAX_AGE_S. projects() returns the
    folders; log gets one line per run and every failure (a failing project never stops the others)."""

    def __init__(self, projects, log, interval=SCHEDULE_S, first_delay=FIRST_RUN_DELAY_S):
        self._projects, self._log, self._interval, self._first_delay = projects, log, interval, first_delay
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="setup-audit", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run_once(self):
        """Audits each project once; returns {project: summary or error text}."""
        outcome, projects = {}, list(self._projects())
        prune_index(projects)
        for root in projects:
            if self._stop.is_set():
                break
            if not os.path.isdir(root):
                continue
            try:
                report = run(root, probe_max_age=PROBE_MAX_AGE_S, trigger="schedule")
                outcome[root] = report["summary"]
            except Exception as exc:
                outcome[root] = f"{type(exc).__name__}: {exc}"[:200]
                self._log(f"setup audit failed for {root}: {outcome[root]}")
        errors = sum(v["error"] for v in outcome.values() if isinstance(v, dict))
        self._log(f"setup audit: {len(outcome)} project(s), {errors} error finding(s)")
        return outcome

    def _loop(self):
        if self._stop.wait(self._first_delay):
            return
        while True:
            self.run_once()
            if self._stop.wait(self._interval):
                return


def markdown(report):
    """The report as Markdown for people: findings by severity, what changed, what was not verified."""
    s = report["summary"]
    lines = [f"# Setup audit: {report['project']}", "",
             f"{s['error']} errors, {s['warn']} warnings, {s['info']} notes. Snapshot {report['snapshot_key']} "
             f"({report['files']} files read).", ""]
    changes = report.get("changes")
    if changes:
        lines += [f"Since the last run: {len(changes['new'])} new, {len(changes['resolved'])} resolved"
                  + (" (configuration changed)." if changes["snapshot_changed"] else "."), ""]
    for finding in report["findings"]:
        mark = {"error": "ERROR", "warn": "WARN", "info": "NOTE"}[finding["severity"]]
        lines.append(f"- **{mark}** [{finding['harness']}] {finding['subject']}: {finding['detail']} "
                     f"([source]({finding['source']}))" + (" *(new)*" if finding.get("new") else ""))
    for side in ("claude", "codex"):
        servers = [m for m in report[side]["mcp"] if "tools" in m]
        if servers:
            lines += ["", f"MCP tools offered to {side}: " + ", ".join(
                f"{m['name']} {m['tools']} (~{m['tool_chars'] // 4:,} tokens)" for m in servers)]
    lines += ["", "Not verified:"] + [f"- {item}" for item in report["unverified"]]
    return "\n".join(lines)


def main():
    """CLI: prints the Markdown report (or JSON with --json)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(args.project, probe=not args.no_probe)
    print(json.dumps(report, indent=1, ensure_ascii=False) if args.json else markdown(report))


if __name__ == "__main__":
    main()
