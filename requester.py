"""Who asked for a web search or fetch, recorded next to each provider call and research job, never the query text.

An MCP client only sends `clientInfo` (name, version) and the daemon's own `Mcp-Session-Id`: no session or folder. The
PreToolUse hook of Claude Code does carry `session_id` and `cwd` before every tool call it matches, so the hook route
notes them here (note_hook) and the MCP call that follows claims them by tool and arguments (claim). The first claim
binds that MCP session to the Claude session and folder, so later calls of the same MCP session are attributed even
when the hook did not fire for them. A client without that hook (Codex, or a Claude Code whose hook predates the
web tools) leaves `session` and `project` empty and `source` says how far the attribution got.
"""
import collections
import contextvars
import json
import threading
import time

WEB_TOOLS = ("web_search", "web_fetch")
NOTE_TTL_S = 60
MAX_NOTES = 256
MAX_BOUND = 256

CURRENT = contextvars.ContextVar("requester", default=None)
_LOCK = threading.Lock()
_NOTES = collections.OrderedDict()
_BOUND = collections.OrderedDict()


def _key(tool, arguments):
    return tool + "\0" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def note_hook(tool, arguments, session_id, cwd):
    """Remembers who the PreToolUse hook saw about to call `tool` with `arguments` (the MCP tool name without the
    server prefix); claim() takes it back within NOTE_TTL_S."""
    if tool not in WEB_TOOLS or not isinstance(arguments, dict):
        return
    entry = (time.monotonic(), session_id if isinstance(session_id, str) else None, cwd if isinstance(cwd, str) else None)
    with _LOCK:
        _NOTES.setdefault(_key(tool, arguments), collections.deque()).append(entry)
        while len(_NOTES) > MAX_NOTES:
            _NOTES.popitem(last=False)


def claim(mcp_session, client, tool, arguments):
    """The requester of one MCP tool call: {client, mcp_session, session, project, source}. source is `hook` when the
    hook noted this very call, `mcp-session` when only an earlier call of the same MCP session was attributed, `none`
    when the client gave nothing beyond its name."""
    now = time.monotonic()
    found = None
    with _LOCK:
        queue = _NOTES.get(_key(tool, arguments))
        while queue and now - queue[0][0] > NOTE_TTL_S:
            queue.popleft()
        if queue:
            found = queue.popleft()
        if queue is not None and not queue:
            del _NOTES[_key(tool, arguments)]
        if found and mcp_session:
            _BOUND[mcp_session] = (found[1], found[2])
            while len(_BOUND) > MAX_BOUND:
                _BOUND.popitem(last=False)
        bound = _BOUND.get(mcp_session)
    session, project = (found[1:] if found else bound) or (None, None)
    return {"client": client or None, "mcp_session": (mcp_session or "")[:12] or None, "session": session,
            "project": project, "source": "hook" if found else ("mcp-session" if bound else "none")}


def from_hook(client, payload):
    """The requester of a native WebSearch/WebFetch the hook is about to redirect: the payload itself names the session
    and folder, no MCP call is involved."""
    return {"client": client or None, "mcp_session": None, "session": payload.get("session_id"),
            "project": payload.get("cwd"), "source": "hook"}


def current():
    """The requester of the tool call this thread (or a context copied from it) is serving, or None outside one."""
    return CURRENT.get()
