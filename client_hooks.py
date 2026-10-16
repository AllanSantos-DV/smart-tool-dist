import json
import os
import secrets
import shutil
import sys
import time

import daemon_launcher
import endpoint_sync

HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "pretool_router.py")
MARKER = "pretool_router.py"
AUDIT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "session_audit.py")
AUDIT_MARKER = "session_audit.py"
AUDIT_MATCHER = "startup|resume|clear"
AUDIT_TIMEOUT_S = 10
HTTP_PATH = "/hooks/pretool"
HTTP_TIMEOUT_S = 6
HOME = os.path.expanduser("~")
PREVIEW_TTL_S = 600
CLIENTS = {
    "claude": {"label": "Claude Code", "names": {"claude-code"}, "files": [os.path.join(endpoint_sync.claude_dir(), "settings.json")],
               "docs": "https://code.claude.com/docs/en/hooks", "event": "PreToolUse", "matcher": "Grep|Glob|Read|Bash|PowerShell|WebSearch|WebFetch|Edit|MultiEdit|Write|mcp__smart-tool__smart_search", "http": True},
    "codex": {"label": "Codex", "prefix": "codex", "files": [os.path.join(endpoint_sync.codex_dir(), "hooks.json"), os.path.join(endpoint_sync.codex_dir(), "config.toml")],
              "docs": "https://learn.chatgpt.com/docs/hooks", "event": "PreToolUse", "matcher": "Bash|apply_patch",
              "after_install": "Codex only runs new hooks after the user approves the definition in /hooks."},
    "cursor": {"label": "Cursor", "prefix": "cursor", "files": [os.path.join(HOME, ".cursor", "hooks.json")],
               "docs": "https://cursor.com/docs/agent/hooks", "event": "preToolUse", "matcher": "Shell|Read|Grep",
               "blocked": "In Cursor, `allow` approves the tool without the user and empty output blocks: there is no documented neutral "
                          "response, so the hook is not installed automatically."},
}
_PREVIEWS = {}


def identify(client_info):
    name = str((client_info or {}).get("name") or "").casefold()
    return next((key for key, spec in CLIENTS.items()
                 if name in spec.get("names", ()) or (spec.get("prefix") and name.startswith(spec["prefix"]))), None)


def _our_hook(hook):
    return isinstance(hook, dict) and (MARKER in str(hook.get("command", "")) or HTTP_PATH in str(hook.get("url", "")))


def _ours(group):
    return isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(map(_our_hook, group["hooks"]))


def _without_ours(groups):
    """Drops only our hook entries; a group shared with other hooks keeps them and disappears only when emptied."""
    kept = []
    for group in groups:
        if not _ours(group):
            kept.append(group)
            continue
        others = [hook for hook in group["hooks"] if not _our_hook(hook)]
        if others:
            kept.append({**group, "hooks": others})
    return kept


def _target(handler):
    return handler.get("url") or handler.get("command") or ""


def inspect(client):
    """Our hook in the client's config: `current`, `missing`, or `outdated` (an older entry of ours) with `changes`,
    plain sentences saying what an update rewrites (tools the old matcher leaves out, an old address or timeout)."""
    spec = CLIENTS[client]
    for path in spec["files"]:
        try:
            with open(path, encoding="utf-8") as stream:
                text = stream.read()
        except FileNotFoundError:
            continue
        if MARKER not in text and HTTP_PATH not in text:
            continue
        if not path.endswith(".json"):
            return {"state": "current", "changes": []}
        data = json.loads(text)
        hooks = data.get("hooks") if isinstance(data, dict) else None
        groups = hooks.get(spec["event"]) if isinstance(hooks, dict) else None
        ours = [group for group in groups if _ours(group)] if isinstance(groups, list) else []
        if not ours:
            return {"state": "outdated", "changes": [f"Smart Tool's entry is not under {spec['event']}."]}
        expected = _handler(client)
        if any(group.get("matcher") == spec["matcher"] and expected in group["hooks"] for group in ours):
            return {"state": "current", "changes": []}
        group = ours[0]
        changes = []
        covered = str(group.get("matcher") or "").split("|")
        left_out = [tool for tool in spec["matcher"].split("|") if tool not in covered]
        if left_out:
            changes.append("It does not cover " + ", ".join(left_out) + ".")
        found = next(hook for hook in group["hooks"] if _our_hook(hook))
        if _target(found) != _target(expected):
            changes.append(f"It calls {_target(found)} instead of {_target(expected)}.")
        elif found != expected:
            changes.append("Its timeout or type is older than the current one.")
        if not changes:
            changes.append("Its tool list is ordered or written differently from the current one.")
        return {"state": "outdated", "changes": changes}
    return {"state": "missing", "changes": []}


def installed(client):
    """Our hook is present and current; an older entry of ours counts as not installed."""
    return inspect(client)["state"] == "current"


def status(client_info):
    """Hook state for an MCP client's clientInfo: `installed` (bool, None when unreadable or unknown client), `state`
    (current/outdated/missing), `changes` an update would make, `error`, the config `files` and `docs`."""
    client = identify(client_info)
    if client is None:
        return {"client": None, "name": (client_info or {}).get("name"), "installed": None, "error": None}
    spec = CLIENTS[client]
    try:
        found, error = inspect(client), None
    except (OSError, ValueError) as exc:
        found, error = {"state": None, "changes": []}, f"{type(exc).__name__}: {exc}"[:200]
    return {"client": client, "name": client_info.get("name"),
            "installed": None if found["state"] is None else found["state"] == "current", "state": found["state"],
            "changes": found["changes"], "error": error, "files": spec["files"], "docs": spec["docs"]}


def notice(state):
    """Sentence for the agent when the hook is not current (from `status`), or None when nothing needs saying."""
    if state["client"] is None:
        return (f"Unrecognized MCP client ({state['name'] or 'no clientInfo'}): Smart Tool does not check the hook "
                "that redirects Grep/Read to smart_search and WebSearch/WebFetch to web_search/web_fetch in this client.")
    spec = CLIENTS[state["client"]]
    if state["error"]:
        return f"Could not check the Smart Tool hook in {spec['files'][0]}: {state['error']}"
    if state["installed"]:
        return None
    action = spec.get("blocked") or ("If the user asks, use project_manage action=install_hook_preview, show the "
                                     "change and confirm with action=install_hook (makes a backup).")
    if state.get("state") == "outdated":
        return (f"Smart Tool hook outdated in {spec['label']} ({spec['files'][0]}): {' '.join(state['changes'])} "
                f"{action} Docs: {spec['docs']}")
    return (f"Smart Tool hook missing in {spec['label']} ({spec['files'][0]}): Grep/Read/Bash are not "
            f"redirected to smart_search, nor WebSearch/WebFetch to web_search/web_fetch. {action} Docs: {spec['docs']}")


def _shell_safe(path):
    """Unquoted path that runs the same in cmd, PowerShell and bash (the Codex docs don't name the shell): forward
    slashes (bash eats backslashes), no spaces (8.3 short name), no quotes (PowerShell reads a quoted head as a string)."""
    if " " in path and sys.platform == "win32":
        import ctypes
        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
            path = buffer.value
    if " " in path:
        raise ValueError(f"Path contains a space and has no 8.3 short name ({path}); the hook was not installed.")
    return path.replace("\\", "/")


def _command(client):
    return f"{_shell_safe(sys.executable)} {_shell_safe(HOOK_SCRIPT)} --client {client}"


def _handler(client):
    """Claude Code posts the payload straight to the warm daemon (`type: "http"`, no process per tool call); other
    clients run the thin command that forwards to the same route."""
    if CLIENTS[client].get("http"):
        registry = daemon_launcher.read_registry()
        if not registry or not registry.get("url"):
            raise ValueError("Smart Tool daemon has no registered URL; start the daemon before installing the hook.")
        return {"type": "http", "url": f"{registry['url']}{HTTP_PATH}?client={client}", "timeout": HTTP_TIMEOUT_S}
    return {"type": "command", "command": _command(client), "timeout": HTTP_TIMEOUT_S}


def _plan(client_info):
    client = identify(client_info)
    if client is None:
        raise ValueError(f"Unrecognized MCP client ({(client_info or {}).get('name') or 'no clientInfo'}); "
                         "the hook is only installed for Claude Code or Codex, from that client's own session.")
    spec = CLIENTS[client]
    if spec.get("blocked"):
        raise ValueError(spec["blocked"] + f" Configure it manually if you want: {spec['docs']}")
    path = os.path.realpath(spec["files"][0])
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.setdefault("hooks", {}), dict):
        raise ValueError(f"{path} does not have the expected format (object with 'hooks'); nothing was changed.")
    groups = data["hooks"].setdefault(spec["event"], [])
    if not isinstance(groups, list):
        raise ValueError(f"{path}: hooks.{spec['event']} is not a list; nothing was changed.")
    groups[:] = _without_ours(groups)
    entry = {"matcher": spec["matcher"], "hooks": [_handler(client)]}
    return client, spec, path, data, groups, entry


def preview(client_info):
    client, spec, path, _data, _groups, entry = _plan(client_info)
    if installed(client):
        return {"client": client, "installed": True, "file": path, "change": None}
    token = secrets.token_urlsafe(12)
    _PREVIEWS[token] = {"client": client, "at": time.time()}
    return {"client": client, "installed": False, "file": path, "event": spec["event"], "change": entry,
            "token": token, "expires_s": PREVIEW_TTL_S, "after_install": spec.get("after_install")}


def install(client_info, token, confirm):
    """Writes the redirect hook for client_info's client with a backup; needs the `preview` token of this session
    (10 min) and confirm=True. Returns {client, installed, changed, file, backup}."""
    planned = _PREVIEWS.pop(token or "", None)
    client, spec, path, data, groups, entry = _plan(client_info)
    if confirm is not True or not planned or planned["client"] != client or time.time() - planned["at"] > PREVIEW_TTL_S:
        raise ValueError("Installing requires the install_hook_preview token from this session (valid for 10 min) and confirm=true.")
    if installed(client):
        return {"client": client, "installed": True, "changed": False, "file": path}
    groups.append(entry)
    backup = _write_checked(path, data, lambda: installed(client), "The hook")
    return {"client": client, "installed": True, "changed": True, "file": path, "backup": backup,
            "after_install": spec.get("after_install")}


def _write_checked(path, data, verify, what):
    """Writes data as JSON to path through a temp file after a timestamped backup; when verify() fails on the written
    file, restores the backup (or removes the new file) and raises. Returns the backup path (None for a new file)."""
    backup = None
    if os.path.exists(path):
        backup = f"{path}.smart-tool-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".smart-tool-tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
    os.replace(temporary, path)
    if not verify():
        if backup:
            shutil.copy2(backup, path)
        else:
            os.remove(path)
        raise RuntimeError(f"{what} does not appear in {path} after writing; the file was restored.")
    return backup


def _audit_hook(hook):
    return isinstance(hook, dict) and AUDIT_MARKER in str(hook.get("command", ""))


def _audit_ours(group):
    return isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(map(_audit_hook, group["hooks"]))


def _audit_entry():
    return {"matcher": AUDIT_MATCHER,
            "hooks": [{"type": "command", "command": f"{_shell_safe(sys.executable)} {_shell_safe(AUDIT_SCRIPT)}",
                       "timeout": AUDIT_TIMEOUT_S}]}


def _audit_plan():
    """(path, data, groups) of Claude Code's settings with our SessionStart entry already removed from groups."""
    path = os.path.realpath(CLIENTS["claude"]["files"][0])
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.setdefault("hooks", {}), dict):
        raise ValueError(f"{path} does not have the expected format (object with 'hooks'); nothing was changed.")
    groups = data["hooks"].setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise ValueError(f"{path}: hooks.SessionStart is not a list; nothing was changed.")
    kept = []
    for group in groups:
        if not _audit_ours(group):
            kept.append(group)
            continue
        others = [hook for hook in group["hooks"] if not _audit_hook(hook)]
        if others:
            kept.append({**group, "hooks": others})
    groups[:] = kept
    return path, data, groups


def audit_state():
    """The SessionStart audit hook in Claude Code's settings: `current` (our entry equals this version's), `outdated`
    (an entry of ours that differs) or `missing`."""
    path = CLIENTS["claude"]["files"][0]
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return "missing"
    groups = ((data.get("hooks") or {}).get("SessionStart") or []) if isinstance(data, dict) else []
    found = [group for group in groups if _audit_ours(group)] if isinstance(groups, list) else []
    if not found:
        return "missing"
    return "current" if _audit_entry() in found else "outdated"


def audit_hook_installed(client):
    """The Claude Code session-start audit report is on, so a search need not repeat it. An unreadable settings file
    counts as off here; `audit_state` raises for it on the setup screen."""
    if client != "claude":
        return False
    try:
        return audit_state() == "current"
    except (OSError, ValueError):
        return False


def audit_preview():
    """The change that turns the SessionStart audit report on in Claude Code, with a token for `audit_install`."""
    path, _data, _groups = _audit_plan()
    entry = _audit_entry()
    if audit_state() == "current":
        return {"installed": True, "file": path, "change": None}
    token = secrets.token_urlsafe(12)
    _PREVIEWS[token] = {"client": "audit", "at": time.time()}
    return {"installed": False, "file": path, "event": "SessionStart", "change": entry, "token": token,
            "expires_s": PREVIEW_TTL_S}


def audit_install(token, confirm):
    """Writes the SessionStart audit entry into Claude Code's settings with a backup; needs the `audit_preview` token
    (10 min) and confirm=True. Returns {installed, changed, file, backup}."""
    planned = _PREVIEWS.pop(token or "", None)
    if confirm is not True or not planned or planned["client"] != "audit" or time.time() - planned["at"] > PREVIEW_TTL_S:
        raise ValueError("Turning the audit report on requires the preview token (valid for 10 min) and confirm=true.")
    path, data, groups = _audit_plan()
    if audit_state() == "current":
        return {"installed": True, "changed": False, "file": path}
    groups.append(_audit_entry())
    backup = _write_checked(path, data, lambda: audit_state() == "current", "The audit hook")
    return {"installed": True, "changed": True, "file": path, "backup": backup}


def audit_remove():
    """Takes only our SessionStart entry out of Claude Code's settings (after a backup); nothing else is touched."""
    path, data, groups = _audit_plan()
    if audit_state() == "missing":
        return {"installed": False, "changed": False, "file": path}
    if not groups:
        del data["hooks"]["SessionStart"]
    backup = _write_checked(path, data, lambda: audit_state() == "missing", "The removal")
    return {"installed": False, "changed": True, "file": path, "backup": backup}
