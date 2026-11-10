import hashlib
import json
import os
import secrets
import shutil
import sys
import time
import tomllib

import atomic_io
import codex_config
import daemon_launcher
import endpoint_sync

HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "pretool_router.py")
MARKER = "pretool_router.py"
AUDIT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "session_audit.py")
AUDIT_MARKER = "session_audit.py"
# Hook origin convention shared by every project that installs hooks: a failed command hook names its owner.
HOOK_ORIGIN = "--hook-origin=smart-tool"
AUDIT_MATCHER = "startup|resume|clear|compact|fork"
AUDIT_TIMEOUT_S = 10
AUDIT_CLIENTS = ("claude", "codex")
AUDIT_ON = "current"
AUDIT_WAITING = "waiting_approval"
HTTP_PATH = "/hooks/pretool"
POST_PATH = "/hooks/posttool"
# A redirected shell search waits for its answer in the PreToolUse (smart_tool_daemon.HOOK_SHELL_BUDGET_S).
HTTP_TIMEOUT_S = 15
# The PostToolUse waits for the Smart Tool answer of a redirected call (web_fetch reads a whole page).
POST_TIMEOUT_S = 60
HOME = os.path.expanduser("~")
PREVIEW_TTL_S = 600
CLIENTS = {
    "claude": {"label": "Claude Code", "names": {"claude-code"}, "files": [os.path.join(endpoint_sync.claude_dir(), "settings.json")],
               "docs": "https://code.claude.com/docs/en/hooks", "event": "PreToolUse", "matcher": "Grep|Glob|Read|Bash|PowerShell|WebSearch|WebFetch|Edit|MultiEdit|Write|mcp__smart-tool__smart_search|mcp__smart-tool__web_search|mcp__smart-tool__web_fetch", "http": True,
               "post_matcher": "Grep|Bash|PowerShell|WebFetch|WebSearch"},
    "codex": {"label": "Codex", "prefix": "codex", "files": [os.path.join(endpoint_sync.codex_dir(), "hooks.json"), os.path.join(endpoint_sync.codex_dir(), "config.toml")],
              "docs": "https://learn.chatgpt.com/docs/hooks", "event": "PreToolUse", "matcher": "Bash|apply_patch",
              "after_install": "Smart Tool recorded the approval of the new definition in Codex's config.toml, as /hooks "
                               "would, so Codex runs it from the next session on."},
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
    return isinstance(hook, dict) and (MARKER in str(hook.get("command", "")) or
                                       any(path in str(hook.get("url", "")) for path in (HTTP_PATH, POST_PATH)))


def _entries(client):
    """{event: (matcher, handler)} of our hook entries for the client: the PreToolUse, plus the PostToolUse that returns
    the Smart Tool answer of a redirected call in Claude Code."""
    spec = CLIENTS[client]
    entries = {spec["event"]: (spec["matcher"], _handler(client))}
    if spec.get("post_matcher"):
        entries["PostToolUse"] = (spec["post_matcher"], _handler(client, POST_PATH, POST_TIMEOUT_S))
    return entries


def _ours(group, is_ours=_our_hook):
    return isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(map(is_ours, group["hooks"]))


def _without_ours(groups, is_ours=_our_hook):
    """Drops only our hook entries; a group shared with other hooks keeps them and disappears only when emptied."""
    kept = []
    for group in groups:
        if not _ours(group, is_ours):
            kept.append(group)
            continue
        others = [hook for hook in group["hooks"] if not is_ours(hook)]
        if others:
            kept.append({**group, "hooks": others})
    return kept


def _target(handler):
    return handler.get("url") or handler.get("command") or ""


def inspect(client, trust=True):
    """Our hook in the client's config: `current`, `missing`, or `outdated` (an older entry of ours) with `changes`,
    plain sentences saying what an update rewrites (tools the old matcher leaves out, an old address or timeout).
    In Codex a current entry that config.toml does not trust is outdated too, unless trust=False."""
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
        changes = []
        for event, (matcher, expected) in _entries(client).items():
            groups = hooks.get(event) if isinstance(hooks, dict) else None
            changes += _entry_changes(event, matcher, expected, groups)
            if client == "codex" and trust and not changes and not _codex_trusted(event, groups, _our_hook):
                changes.append(f"Codex has not approved its {event} entry; an update records the approval.")
        return {"state": "outdated" if changes else "current", "changes": changes}
    return {"state": "missing", "changes": []}


def _entry_changes(event, matcher, expected, groups):
    """Sentences saying what an update rewrites in our entry under one event; empty when it is current."""
    ours = [group for group in groups if _ours(group)] if isinstance(groups, list) else []
    if not ours:
        return [f"Smart Tool's entry is not under {event}."]
    if any(group.get("matcher") == matcher and expected in group["hooks"] for group in ours):
        return []
    group = ours[0]
    changes = []
    covered = str(group.get("matcher") or "").split("|")
    left_out = [tool for tool in matcher.split("|") if tool not in covered]
    if left_out:
        changes.append(f"Its {event} entry does not cover {', '.join(left_out)}.")
    found = next(hook for hook in group["hooks"] if _our_hook(hook))
    if _target(found) != _target(expected):
        changes.append(f"It calls {_target(found)} instead of {_target(expected)}.")
    elif found != expected:
        changes.append("Its timeout or type is older than the current one.")
    if not changes:
        changes.append("Its tool list is ordered or written differently from the current one.")
    return changes


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
    """The PreToolUse command of a client without HTTP hooks, signed with HOOK_ORIGIN at the end."""
    return f"{_shell_safe(sys.executable)} {_shell_safe(HOOK_SCRIPT)} --client {client} {HOOK_ORIGIN}"


def _handler(client, route=HTTP_PATH, timeout=HTTP_TIMEOUT_S):
    """Claude Code posts the payload straight to the warm daemon (`type: "http"`, no process per tool call); other
    clients run the thin command that forwards to the PreToolUse route."""
    if CLIENTS[client].get("http"):
        registry = daemon_launcher.read_registry()
        if not registry or not registry.get("url"):
            raise ValueError("Smart Tool daemon has no registered URL; start the daemon before installing the hook.")
        return {"type": "http", "url": f"{registry['url']}{route}?client={client}", "timeout": timeout}
    return {"type": "command", "command": _command(client), "timeout": timeout}


def _slot(groups, is_ours):
    """Index, among the groups left once ours are taken out, where our replacement entry goes: the place of our first
    group that holds only our hooks, so the groups after it keep their index (Codex keys an approval by that index);
    the end when there is no such group."""
    for kept, group in enumerate(groups):
        if _ours(group, is_ours) and all(map(is_ours, group["hooks"])):
            return kept
    return len(_without_ours(groups, is_ours))


def _plan(client_info):
    """(client, spec, path, data, changes): the client's config with our entries already taken out, and changes
    {event: (groups, entry, slot, before)}: the entry to insert at `slot` of each event's groups, and the groups as
    they were before."""
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
    changes = {}
    for event, (matcher, handler) in _entries(client).items():
        groups = data["hooks"].setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"{path}: hooks.{event} is not a list; nothing was changed.")
        before, slot = list(groups), _slot(groups, _our_hook)
        groups[:] = _without_ours(groups)
        changes[event] = (groups, {"matcher": matcher, "hooks": [handler]}, slot, before)
    return client, spec, path, data, changes


def preview(client_info):
    """The entries install would write for client_info's client ({event: entry} in `change`, None when current) and
    the token `install` needs."""
    client, spec, path, _data, changes = _plan(client_info)
    if installed(client):
        return {"client": client, "installed": True, "file": path, "change": None}
    token = secrets.token_urlsafe(12)
    _PREVIEWS[token] = {"client": client, "at": time.time()}
    return {"client": client, "installed": False, "file": path,
            "change": {event: entry for event, (_groups, entry, _slot, _before) in changes.items()},
            "token": token, "expires_s": PREVIEW_TTL_S, "after_install": spec.get("after_install")}


def install(client_info, token, confirm):
    """Writes the redirect hook for client_info's client with a backup; needs the `preview` token of this session
    (10 min) and confirm=True. Returns {client, installed, changed, file, backup}."""
    planned = _PREVIEWS.pop(token or "", None)
    client, spec, path, data, changes = _plan(client_info)
    if confirm is not True or not planned or planned["client"] != client or time.time() - planned["at"] > PREVIEW_TTL_S:
        raise ValueError("Installing requires the install_hook_preview token from this session (valid for 10 min) and confirm=true.")
    if installed(client):
        return {"client": client, "installed": True, "changed": False, "file": path}
    if client == "codex":
        _codex_states()
    for groups, entry, slot, _before in changes.values():
        groups.insert(slot, entry)
    backup = _write_checked(path, data, lambda: inspect(client, trust=False)["state"] == "current", "The hook")
    if client == "codex":
        record_codex_trust({key: value for event, (groups, _entry, _slot, before) in changes.items()
                      for key, value in _codex_trust(event, before, groups).items()})
        if not installed(client):
            raise RuntimeError(f"Codex does not trust the hook in {path} after recording its approval.")
    return {"client": client, "installed": True, "changed": True, "file": path, "backup": backup,
            "after_install": spec.get("after_install")}


def _write_checked(path, data, verify, what):
    """Writes data as JSON to path through a temp file after a timestamped backup; when verify() fails on the written
    file, restores the backup (or removes the new file) and raises. Returns the backup path (None for a new file)."""
    backup = atomic_io.backup_file(path)
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


def _audit_entry():
    """The SessionStart group of the session-start hook, its command signed with HOOK_ORIGIN at the end."""
    return {"matcher": AUDIT_MATCHER,
            "hooks": [{"type": "command",
                       "command": f"{_shell_safe(sys.executable)} {_shell_safe(AUDIT_SCRIPT)} {HOOK_ORIGIN}",
                       "timeout": AUDIT_TIMEOUT_S}]}


def _audit_client(client):
    """The client's config spec; only Claude Code and Codex fire a SessionStart hook whose output the model reads."""
    if client not in AUDIT_CLIENTS:
        raise ValueError(f"The session-start audit report is only available for Claude Code or Codex, not {client!r}.")
    return CLIENTS[client]


def _audit_plan(client="claude"):
    """(path, data, groups, slot, before) of the client's hooks file: groups with our SessionStart entry already
    removed, the slot where a replacement goes and the groups as they were before."""
    path = os.path.realpath(_audit_client(client)["files"][0])
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
    before, slot = list(groups), _slot(groups, _audit_hook)
    groups[:] = _without_ours(groups, _audit_hook)
    return path, data, groups, slot, before


def _codex_hash(event, group, index=0):
    """Codex's approval hash (`sha256:` of the key-sorted compact JSON of the normalized hook identity, as in
    openai/codex hooks/src/engine/discovery.rs `hook_hash`) of the handler at `index` in a hooks.json group."""
    handler = group["hooks"][index]
    normalized = {"type": handler["type"], "command": handler["command"], "timeout": handler.get("timeout", 600),
                  "async": bool(handler.get("async", False))}
    if handler.get("statusMessage") is not None:
        normalized["statusMessage"] = handler["statusMessage"]
    identity = {"event_name": event, "hooks": [normalized]}
    if group.get("matcher") is not None:
        identity["matcher"] = group["matcher"]
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _codex_event(event):
    """Codex's snake_case name of a hooks.json event (`PreToolUse` -> `pre_tool_use`), as in its state keys."""
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in event).lstrip("_")


def _codex_key(event, group_index, handler_index, hooks=None):
    """Codex's `hooks.state` key of one handler of `hooks` (default: Codex's hooks.json): file path, event, group
    index, handler index."""
    return f"{hooks or CLIENTS['codex']['files'][0]}:{_codex_event(event)}:{group_index}:{handler_index}"


def _codex_states(path=None):
    """The `hooks.state` table of Codex's config.toml at `path` (default: Codex's), {} when the file or the table is
    missing; a config.toml that is not valid TOML raises ValueError."""
    path = path or CLIENTS["codex"]["files"][1]
    try:
        with open(path, encoding="utf-8") as stream:
            hooks = tomllib.loads(stream.read()).get("hooks")
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}")
    states = hooks.get("state") if isinstance(hooks, dict) else None
    return states if isinstance(states, dict) else {}


def _codex_trusted(event, groups, is_ours, hooks_path=None, config_path=None):
    """Our handler under `event` is trusted in Codex's config.toml: the entry keyed by hooks.json path, event, group
    and handler index holds the hash of the definition as it is now (a missing or different hash means Codex does not
    run it). The paths default to Codex's own. An unreadable config.toml raises ValueError."""
    states = _codex_states(config_path)
    name = _codex_event(event)
    path = hooks_path or CLIENTS["codex"]["files"][0]
    for group_index, group in enumerate(groups):
        if not _ours(group, is_ours):
            continue
        handler_index = next(i for i, hook in enumerate(group["hooks"]) if is_ours(hook))
        for prefix in {path, os.path.realpath(path)}:
            trusted = (states.get(f"{prefix}:{name}:{group_index}:{handler_index}") or {}).get("trusted_hash")
            if trusted == _codex_hash(name, group, handler_index):
                return True
    return False


def _command_handlers(groups):
    """(group index, handler index, group, handler) of every command handler in an event's groups."""
    return [(group_index, handler_index, group, hook) for group_index, group in enumerate(groups)
            if isinstance(group, dict) and isinstance(group.get("hooks"), list)
            for handler_index, hook in enumerate(group["hooks"])
            if isinstance(hook, dict) and hook.get("type") == "command" and "command" in hook]


def _codex_trust(event, before, after):
    """{state key: trusted hash} Codex needs once Smart Tool rewrote one event's groups from `before` to `after`: our
    handlers, which the user approved by confirming the change, and every other command handler that moved to a new
    index while config.toml trusts its definition at the old one."""
    states = _codex_states()
    name = _codex_event(event)
    old = {id(hook): (group_index, handler_index, group)
           for group_index, handler_index, group, hook in _command_handlers(before)}
    wanted = {}
    for group_index, handler_index, group, hook in _command_handlers(after):
        key = _codex_key(event, group_index, handler_index)
        if _our_hook(hook) or _audit_hook(hook):
            wanted[key] = _codex_hash(name, group, handler_index)
            continue
        found = old.get(id(hook))
        if found and found[:2] != (group_index, handler_index):
            trusted = (states.get(_codex_key(event, *found[:2])) or {}).get("trusted_hash")
            if trusted == _codex_hash(name, found[2], found[1]):
                wanted[key] = _codex_hash(name, group, handler_index)
    return wanted


def _toml_table(line):
    """Key path of a `[table]` header line (`[hooks.state.'x']` -> ("hooks", "state", "x")), None for any other line."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        table = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(table, dict) and len(table) == 1:
        key = next(iter(table))
        path.append(key)
        table = table[key]
    return tuple(path)


def _with_trusted_hash(lines, key, value, newline):
    """config.toml lines with `trusted_hash = "<value>"` under `[hooks.state.<key>]`: replaced or added inside the
    table when its header exists, else a new table at the end."""
    line = f'trusted_hash = "{value}"{newline}'
    header = next((i for i, text in enumerate(lines) if _toml_table(text) == ("hooks", "state", key)), None)
    if header is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        quoted = f"'{key}'" if "'" not in key else json.dumps(key)
        return lines + [newline, f"[hooks.state.{quoted}]{newline}", line]
    end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(header + 1, end):
        if lines[i].split("=")[0].strip() == "trusted_hash":
            lines[i] = line
            return lines
    lines.insert(header + 1, line)
    return lines


def repoint_trust(hooks_path, before_text, after_text):
    """{state key: hash} for Codex's hooks.json at hooks_path once the installer rewrote it from before_text to
    after_text to point at a new install folder: each handler of ours whose old definition config.toml trusted at the
    same index gets the hash of its new one, so the move does not stop it. An unreadable config.toml raises ValueError."""
    states = _codex_states(os.path.join(os.path.dirname(hooks_path), "config.toml"))
    before = json.loads(before_text).get("hooks") or {}
    wanted = {}
    for event, groups in (json.loads(after_text).get("hooks") or {}).items():
        old = {position[:2]: position for position in _command_handlers(before.get(event) or [])}
        for group_index, handler_index, group, hook in _command_handlers(groups):
            key = _codex_key(event, group_index, handler_index, hooks_path)
            was = old.get((group_index, handler_index))
            if (_our_hook(hook) or _audit_hook(hook)) and was and (states.get(key) or {}).get("trusted_hash") == \
                    _codex_hash(_codex_event(event), was[2], handler_index):
                wanted[key] = _codex_hash(_codex_event(event), group, handler_index)
    return wanted


def record_codex_trust(wanted, path=None):
    """Records Codex's approval of each {state key: hash} in its config.toml at `path` (default: Codex's), as /hooks
    does, after a timestamped backup; every other line keeps its text. A file that does not read back with these hashes
    is restored and raises. Returns the backup path, None when there was nothing to record or no file before."""
    if not wanted:
        return None
    path = path or CLIENTS["codex"]["files"][1]
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            text = stream.read()
    except FileNotFoundError:
        text = ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    for key, value in wanted.items():
        lines = _with_trusted_hash(lines, key, value, newline)
    backup = atomic_io.backup_file(path, keep=codex_config.BACKUPS_KEPT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".smart-tool-tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as stream:
        stream.write("".join(lines))
    os.replace(temporary, path)
    try:
        states = _codex_states(path)
        recorded = all((states.get(key) or {}).get("trusted_hash") == value for key, value in wanted.items())
    except ValueError:
        recorded = False
    if not recorded:
        if backup:
            shutil.copy2(backup, path)
        else:
            os.remove(path)
        raise RuntimeError(f"The hook approval does not read back from {path} after writing; the file was restored.")
    return backup


def audit_state(client="claude"):
    """The SessionStart audit hook in the client's hooks file: `current` (our entry equals this version's and, in
    Codex, config.toml trusts it), `waiting_approval` (Codex only: written but not trusted, so it does not run yet),
    `outdated` (an entry of ours that differs) or `missing`."""
    path = _audit_client(client)["files"][0]
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return "missing"
    groups = ((data.get("hooks") or {}).get("SessionStart") or []) if isinstance(data, dict) else []
    found = [group for group in groups if _ours(group, _audit_hook)] if isinstance(groups, list) else []
    if not found:
        return "missing"
    if _audit_entry() not in found:
        return "outdated"
    return "current" if client != "codex" or _codex_trusted("SessionStart", groups, _audit_hook) else AUDIT_WAITING


def codex_unapproved(hooks_path, config_path):
    """Events whose hook of ours is written in the Codex hooks.json at hooks_path while the config.toml at config_path
    holds no matching approval (`[hooks.state]` trusted_hash), so Codex does not run it. Reads both files, writes
    nothing; [] when hooks.json is missing or holds none of ours. An unreadable config.toml raises ValueError."""
    try:
        with open(hooks_path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    unapproved = []
    for event, is_ours in [*((event, _our_hook) for event in _entries("codex")), ("SessionStart", _audit_hook)]:
        groups = hooks.get(event)
        if isinstance(groups, list) and any(_ours(group, is_ours) for group in groups) \
                and not _codex_trusted(event, groups, is_ours, hooks_path, config_path):
            unapproved.append(event)
    return unapproved


def audit_hook_installed(client):
    """The session-start hook is on and running (Codex: trusted in config.toml), so a search need not repeat the redirect
    proposals it already carries.
    An unreadable config file counts as off here; `audit_state` raises for it on the setup screen."""
    if client not in AUDIT_CLIENTS:
        return False
    try:
        return audit_state(client) == "current"
    except (OSError, ValueError):
        return False


def _audit_after(client):
    """{after_install: ...} for a client whose new hooks run only once approved, else nothing."""
    note = CLIENTS[client].get("after_install")
    return {"after_install": note} if note else {}


def audit_preview(client="claude"):
    """The change that turns the SessionStart audit report on in the client, with a token for `audit_install`."""
    path = _audit_plan(client)[0]
    entry = _audit_entry()
    if audit_state(client) == AUDIT_ON:
        return {"installed": True, "file": path, "change": None}
    token = secrets.token_urlsafe(12)
    _PREVIEWS[token] = {"client": f"audit:{client}", "at": time.time()}
    return {"installed": False, "file": path, "event": "SessionStart", "change": entry, "token": token,
            "expires_s": PREVIEW_TTL_S, **_audit_after(client)}


def audit_install(token, confirm, client="claude"):
    """Writes the SessionStart audit entry into the client's hooks file with a backup; needs the `audit_preview` token
    (10 min) and confirm=True; in Codex it also records the approval in config.toml. Returns {installed, changed, file,
    backup}, plus `after_install` in Codex."""
    planned = _PREVIEWS.pop(token or "", None)
    if confirm is not True or not planned or planned["client"] != f"audit:{client}" or time.time() - planned["at"] > PREVIEW_TTL_S:
        raise ValueError("Turning the audit report on requires the preview token (valid for 10 min) and confirm=true.")
    path, data, groups, slot, before = _audit_plan(client)
    if audit_state(client) == AUDIT_ON:
        return {"installed": True, "changed": False, "file": path}
    if client == "codex":
        _codex_states()
    groups.insert(slot, _audit_entry())
    backup = _write_checked(path, data, lambda: audit_state(client) in (AUDIT_ON, AUDIT_WAITING), "The audit hook")
    if client == "codex":
        record_codex_trust(_codex_trust("SessionStart", before, groups))
        if audit_state(client) != AUDIT_ON:
            raise RuntimeError(f"Codex does not trust the audit hook in {path} after recording its approval.")
    return {"installed": True, "changed": True, "file": path, "backup": backup, **_audit_after(client)}


def audit_remove(client="claude"):
    """Takes only our SessionStart entry out of the client's hooks file (after a backup); in Codex the hooks after it
    keep their approval at their new index. Nothing else is touched."""
    path, data, groups, _slot_at, before = _audit_plan(client)
    if audit_state(client) == "missing":
        return {"installed": False, "changed": False, "file": path}
    if not groups:
        del data["hooks"]["SessionStart"]
    backup = _write_checked(path, data, lambda: audit_state(client) == "missing", "The removal")
    if client == "codex":
        record_codex_trust(_codex_trust("SessionStart", before, groups))
    return {"installed": False, "changed": True, "file": path, "backup": backup}
