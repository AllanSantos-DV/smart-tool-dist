"""Playwright MCP over the daemon's HTTP server, for clients that keep one stdio server per conversation.

Codex starts every registered stdio server once per conversation thread, so each open thread held its own proxy
process even when it never opened a browser. Codex registers `playwright` by URL instead (browser_control) and this
bridge answers it at `/playwright/mcp` as an MCP Streamable HTTP server (JSON replies): `initialize`, `ping` and
`tools/list` come from the handshake cache browser_control wrote, and the first tool call starts ONE shared Playwright
MCP server (its own HTTP mode, `--isolated`: every MCP session gets a browser context of its own) that all client
sessions use through a child session each. The child is killed with its process tree after IDLE_S without a request,
and when the daemon exits (on Windows a kill-on-close job ends it even when the daemon is killed hard); the next tool
call starts it again.

`browser_run_code_unsafe` and the rest of browser_control.DENIED_TOOLS are neither listed nor callable here.
"""
import http.client
import json
import os
import socket
import subprocess
import threading
import time
import uuid

import browser_control
import lazy_mcp_proxy
import paths
import process_limits

PATH = browser_control.BRIDGE_PATH
IDLE_S = 3600
START_TIMEOUT_S = lazy_mcp_proxy.INIT_TIMEOUT_S
REQUEST_TIMEOUT_S = 300
LOG_PATH = os.path.join(paths.DATA_DIR, "playwright-bridge.log")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ACCEPT = "application/json, text/event-stream"
_INIT_ID = "smart-tool-bridge-init"
_DEFAULT_CLIENT = {"protocolVersion": lazy_mcp_proxy.KNOWN_PROTOCOLS[2], "capabilities": {},
                   "clientInfo": {"name": "smart-tool-bridge", "version": "1"}}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message[:500]}}


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _log_tail(limit=300):
    try:
        return process_limits.file_tail(LOG_PATH, limit)
    except OSError:
        return ""


def _matching(message, request_id):
    """The JSON-RPC response in message (a dict, or a batch list) for request_id, else None."""
    for item in message if isinstance(message, list) else [message]:
        if isinstance(item, dict) and item.get("id") == request_id and ("result" in item or "error" in item):
            return item
    return None


def _read_reply(response, request_id):
    """The response to request_id in an HTTP reply that is plain JSON or an event stream whose `data:` events hold
    JSON-RPC messages (notifications may precede the response). RuntimeError when there is none."""
    if not response.getheader("Content-Type", "").startswith("text/event-stream"):
        found = _matching(json.loads(response.read()), request_id)
    else:
        found, data = None, []
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line and data:
                try:
                    found = _matching(json.loads("\n".join(data)), request_id)
                except ValueError:
                    pass
                data = []
                if found:
                    break
    if found is None:
        raise RuntimeError(f"the browser server sent no response to request {request_id!r}")
    return found


class _Child:
    """The shared Playwright MCP server: its process, its port, and the child session id it gave each client session."""

    def __init__(self, proc, port):
        self.proc, self.port = proc, port
        self.sessions = {}
        self.gone = threading.Event()

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        lazy_mcp_proxy.Proxy.kill(self.proc)
        self.proc.wait(10)
        self.gone.set()

    def request(self, method, session, message=None):
        """One HTTP request to the child's MCP endpoint; returns (Mcp-Session-Id header, JSON-RPC response or None).
        RuntimeError on an HTTP error status or a missing response, OSError when the child cannot be reached."""
        headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
        if session:
            headers["Mcp-Session-Id"] = session
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=REQUEST_TIMEOUT_S)
        try:
            connection.request(method, "/mcp", json.dumps(message) if message else None, headers)
            response = connection.getresponse()
            if response.status >= 300:
                raise RuntimeError(f"the browser server answered HTTP {response.status}: "
                                   f"{response.read(300).decode('utf-8', 'replace').strip()}")
            if not message or "id" not in message:
                response.read()
                return response.getheader("Mcp-Session-Id"), None
            return response.getheader("Mcp-Session-Id"), _read_reply(response, message["id"])
        finally:
            connection.close()

    def open_session(self, params):
        """A child session initialized with the client's own initialize params."""
        session, reply = self.request("POST", None, {"jsonrpc": "2.0", "id": _INIT_ID, "method": "initialize",
                                                     "params": params})
        if "error" in reply:
            raise RuntimeError(f"the browser server refused initialize: {reply['error']}")
        if not session:
            raise RuntimeError("the browser server gave no Mcp-Session-Id")
        self.request("POST", session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return session


class Bridge:
    """The HTTP-independent half of the endpoint: `post` and `delete` take what the daemon read from a request and
    return what it must answer. Client sessions live here; the shared child and the child sessions are started when a
    tool call needs them."""

    def __init__(self, idle_s=IDLE_S, clock=time.monotonic, watch=True):
        """idle_s: seconds without a request after which the child stops; clock: monotonic seconds (replaceable in
        tests); watch: whether a thread stops the idle child (False leaves it to `stop_if_idle` calls)."""
        self._idle_s, self._clock, self._watch = idle_s, clock, watch
        self._lock = threading.Lock()
        self._sessions = {}
        self._last_used = clock()
        self.child = None
        self.active = 0
        self._job = None

    def post(self, session_id, message):
        """One JSON-RPC message of a client POST. Returns (HTTP status, extra headers, JSON payload or None).
        Notifications and client responses mean nothing to the bridge: 202. A request whose session is unknown, or
        absent when it is not `initialize`, is 404."""
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize" and request_id is not None:
            return self._initialize(request_id, message.get("params"))
        with self._lock:
            known = session_id in self._sessions
        if not known:
            return 404, {}, _error(request_id, -32600, "unknown or missing Mcp-Session-Id: send initialize first")
        if method is None or request_id is None:
            return 202, {}, None
        if method == "ping":
            return 200, {}, {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return self._answer_tools_list(request_id)
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if method == "tools/call" and name in browser_control.DENIED_TOOLS:
            return 200, {}, _error(request_id, -32602, f"tool {name} is not available through Smart Tool")
        return self._forward(session_id, message)

    def delete(self, session_id):
        """Ends the client session and its child session; 204, or 404 when the session is unknown."""
        with self._lock:
            if self._sessions.pop(session_id, None) is None:
                return 404
            child = self.child
            child_session = child.sessions.pop(session_id, None) if child else None
        if child_session:
            try:
                child.request("DELETE", child_session)
            except (OSError, RuntimeError, http.client.HTTPException):
                pass  # the child is gone or lost the session: its browser context went with it
        return 204

    def stop(self):
        """Kills the shared child with its process tree; client sessions stay and the next tool call starts it again."""
        with self._lock:
            self._drop_child()

    def stop_if_idle(self):
        """Stops the child when no request reached it for the idle time and none is running; True when it stopped."""
        with self._lock:
            if self.child is None or self.active or self._clock() - self._last_used < self._idle_s:
                return False
            self._drop_child()
            return True

    def _initialize(self, request_id, params):
        try:
            handshake = lazy_mcp_proxy.load_handshake(browser_control.HANDSHAKE_PATH)
        except RuntimeError as exc:
            return 200, {}, _error(request_id, -32000, str(exc))
        session_id = uuid.uuid4().hex
        with self._lock:
            self._sessions[session_id] = params if isinstance(params, dict) else _DEFAULT_CLIENT
        result = lazy_mcp_proxy.local_initialize(handshake, params if isinstance(params, dict) else None)
        return 200, {"Mcp-Session-Id": session_id}, {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _answer_tools_list(request_id):
        try:
            tools = lazy_mcp_proxy.load_handshake(browser_control.HANDSHAKE_PATH)["tools"]
        except RuntimeError as exc:
            return 200, {}, _error(request_id, -32000, str(exc))
        tools = [tool for tool in tools if tool.get("name") not in browser_control.DENIED_TOOLS]
        return 200, {}, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}

    def _forward(self, session_id, message):
        """Passes a request to the client session's child session and returns the child's response; a failure at any
        step is a -32000 error that says which."""
        request_id = message["id"]
        try:
            child, child_session = self._acquire(session_id)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as exc:
            return 200, {}, _error(request_id, -32000, f"could not start the browser server: {exc}")
        try:
            _session, reply = child.request("POST", child_session, message)
            return 200, {}, reply
        except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
            return 200, {}, _error(request_id, -32000, f"the browser server failed: {exc}")
        finally:
            self._release()

    def _acquire(self, session_id):
        """The running child and the session's child session (both created when missing), counted as in use until
        `_release`."""
        with self._lock:
            params = self._sessions.get(session_id)
            if params is None:
                raise RuntimeError("the session ended")
            if self.child is not None and not self.child.alive():
                self._drop_child()
            if self.child is None:
                self.child = self._start_child()
            child = self.child
            if session_id not in child.sessions:
                child.sessions[session_id] = child.open_session(params)
            self.active += 1
            self._last_used = self._clock()
            return child, child.sessions[session_id]

    def _release(self):
        with self._lock:
            self.active -= 1
            self._last_used = self._clock()

    def _start_child(self):
        """Starts the shared Playwright MCP server on a free loopback port and waits until it accepts connections;
        RuntimeError (child killed) when it exits or stays closed for START_TIMEOUT_S."""
        command = browser_control.server_command()
        port = _free_port()
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        os.makedirs(browser_control.OUTPUT_DIR, exist_ok=True)
        with open(LOG_PATH, "wb") as log:
            # The server's own host check accepts only the Host header of the address it serves.
            proc = subprocess.Popen([*command, "--port", str(port), "--host", "127.0.0.1",
                                     "--allowed-hosts", f"127.0.0.1:{port}"], cwd=browser_control.OUTPUT_DIR,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=_NO_WINDOW)
        child = _Child(proc, port)
        try:
            self._bind_to_daemon(proc)
            self._wait_for_port(child)
        except BaseException:
            child.kill()
            raise
        if self._watch:
            threading.Thread(target=self._watch_idle, args=(child,), daemon=True).start()
        return child

    def _bind_to_daemon(self, proc):
        """Puts the child in a kill-on-close job, so its tree dies with the daemon even when the daemon is killed hard
        and `stop` never runs. OSError when the system refuses: an unbound child would outlive the daemon."""
        if os.name != "nt":
            return
        self._job = process_limits.bind_to_kill_on_close_job(self._job, proc)

    @staticmethod
    def _wait_for_port(child):
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            code = child.proc.poll()
            if code is not None:
                raise RuntimeError(f"the browser server exited with code {code} before opening its port: {_log_tail()}")
            try:
                socket.create_connection(("127.0.0.1", child.port), timeout=1).close()
                return
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"the browser server did not open its port in {START_TIMEOUT_S}s: {_log_tail()}")
            time.sleep(0.1)

    def _watch_idle(self, child):
        interval = max(0.05, min(30.0, self._idle_s / 4))
        while not child.gone.wait(interval):
            self.stop_if_idle()

    def _drop_child(self):
        """Kills the child and forgets its sessions; the caller holds the lock."""
        child, self.child = self.child, None
        if child is not None:
            child.kill()


BRIDGE = Bridge()
