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
HTTP_PATH = "/hooks/pretool"
HTTP_TIMEOUT_S = 15
HOME = os.path.expanduser("~")
PREVIEW_TTL_S = 600
CLIENTS = {
    "claude": {"label": "Claude Code", "names": {"claude-code"}, "files": [os.path.join(endpoint_sync.claude_dir(), "settings.json")],
               "docs": "https://code.claude.com/docs/en/hooks", "event": "PreToolUse", "matcher": "Grep|Glob|Read|Bash|WebSearch|WebFetch", "http": True},
    "codex": {"label": "Codex", "prefix": "codex", "files": [os.path.join(HOME, ".codex", "hooks.json"), os.path.join(HOME, ".codex", "config.toml")],
              "docs": "https://learn.chatgpt.com/docs/hooks", "event": "PreToolUse", "matcher": "Bash",
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


def installed(client):
    """Our hook is present with the current matcher; an entry with an older matcher counts as missing."""
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
            return True
        data = json.loads(text)
        hooks = data.get("hooks") if isinstance(data, dict) else None
        groups = hooks.get(spec["event"]) if isinstance(hooks, dict) else None
        expected = _handler(client)
        return isinstance(groups, list) and any(
            group.get("matcher") == spec["matcher"] and expected in group["hooks"] for group in groups if _ours(group))
    return False


def status(client_info):
    client = identify(client_info)
    if client is None:
        return {"client": None, "name": (client_info or {}).get("name"), "installed": None, "error": None}
    spec = CLIENTS[client]
    try:
        state, error = installed(client), None
    except (OSError, ValueError) as exc:
        state, error = None, f"{type(exc).__name__}: {exc}"[:200]
    return {"client": client, "name": client_info.get("name"), "installed": state, "error": error,
            "files": spec["files"], "docs": spec["docs"]}


def notice(state):
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
    return (f"Smart Tool hook missing or outdated in {spec['label']} ({spec['files'][0]}): Grep/Read/Bash are not "
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
    planned = _PREVIEWS.pop(token or "", None)
    client, spec, path, data, groups, entry = _plan(client_info)
    if confirm is not True or not planned or planned["client"] != client or time.time() - planned["at"] > PREVIEW_TTL_S:
        raise ValueError("Installing requires the install_hook_preview token from this session (valid for 10 min) and confirm=true.")
    if installed(client):
        return {"client": client, "installed": True, "changed": False, "file": path}
    groups.append(entry)
    backup = None
    if os.path.exists(path):
        backup = f"{path}.smart-tool-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".smart-tool-tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
    os.replace(temporary, path)
    if not installed(client):
        if backup:
            shutil.copy2(backup, path)
        raise RuntimeError(f"The hook does not appear in {path} after writing; the file was restored.")
    return {"client": client, "installed": True, "changed": True, "file": path, "backup": backup,
            "after_install": spec.get("after_install")}
