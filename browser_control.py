"""Browser control for agents: the official Playwright MCP server, registered by default next to smart-tool.

Smart Tool's own browsers only read pages (web_fetch, web_search). Agents that must click, type or log in get
Microsoft's Playwright MCP (pinned in web_adapters/node), driving the Chromium Smart Tool already installs, in an
isolated in-memory profile, headless. Before registering, a probe launches that browser through the server exactly
as the agent would: machines whose policy forbids it (corporate IT blocking remote debugging, a missing browser)
keep today's read-only behavior, and the reason is shown in the setup screen instead of an error. A server named
`playwright` the user already registered is left untouched.

The registered command is lazy_mcp_proxy.py around the real server: every session starts the registered servers, and
a Node process kept idle in each session that never opens a browser cost about 97 MB of commit each. The proxy answers
the handshake from the cache the probe writes (HANDSHAKE_PATH) and starts the real server on the first tool call.

Pages the agent opens are untrusted input. The server never exposes tools a page registers (WebMCP); every request
the browser makes carries AGENT_HEADER, which the daemon refuses, so a page cannot steer the agent into Smart Tool's
own setup page and its token (redirects included); `browser_run_code_unsafe` (arbitrary JavaScript in the server
process, outside any client sandbox) is denied in the client: Playwright MCP itself is not a security boundary and
its documentation points to client-level permissions.
"""
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

import atomic_io
import endpoint_sync
import install_runtime
import lazy_mcp_proxy
import node_locator
import paths

SERVER = "playwright"
CLI = install_runtime.PLAYWRIGHT_MCP_CLI
STATUS_PATH = os.path.join(paths.DATA_DIR, "browser-control.json")
OUTPUT_DIR = os.path.join(paths.DATA_DIR, "playwright-mcp")
CONFIG_PATH = os.path.join(paths.DATA_DIR, "playwright-mcp.json")
HANDSHAKE_PATH = os.path.join(paths.DATA_DIR, "playwright-mcp-handshake.json")
PROXY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lazy_mcp_proxy.py")
AGENT_HEADER = "X-Smart-Tool-Agent-Browser"
OUTPUT_MAX_BYTES = 100 * 1024 * 1024
PROBE_TIMEOUT_S = 60
DENIED_TOOLS = ("browser_run_code_unsafe",)
_CODEX_TABLE_RE = re.compile(rf"^\[mcp_servers\.{SERVER}\][ \t]*\r?\n", re.M)
_CHROMIUM_PROBE = ("from playwright.sync_api import sync_playwright\n"
                   "with sync_playwright() as p: print(p.chromium.executable_path)")


def chromium_executable():
    """The Chromium that `playwright install chromium` put in place for the browser venv; RuntimeError when absent."""
    if not install_runtime.BROWSER_PYTHON.is_file():
        raise RuntimeError(f"Browser runtime missing ({install_runtime.BROWSER_PYTHON}).")
    done = subprocess.run([str(install_runtime.BROWSER_PYTHON), "-c", _CHROMIUM_PROBE], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    path = (done.stdout.strip().splitlines() or [""])[-1]
    if done.returncode or not os.path.isfile(path):
        raise RuntimeError(f"Chromium not found: {(done.stderr or done.stdout).strip()[-300:] or path}")
    return path


def server_command():
    """Absolute command line of the Playwright MCP server, as registered in the clients."""
    node, _npm = install_runtime._node_commands()
    if not node:
        raise RuntimeError("Supported Node.js not found for Playwright MCP.")
    if not CLI.is_file():
        raise RuntimeError(f"Playwright MCP is not installed ({CLI}); reinstall Smart Tool.")
    return [node, str(CLI), "--isolated", "--headless", "--no-webmcp", "--caps=", "--config", write_config(),
            "--executable-path", chromium_executable(), "--output-dir", OUTPUT_DIR,
            "--output-max-size", str(OUTPUT_MAX_BYTES)]


def lazy_command(command):
    """The command registered in the clients: lazy_mcp_proxy.py (console Python of this installation) around command,
    the real server line, answering from HANDSHAKE_PATH until a tool call needs the real one."""
    python = sys.executable
    if os.path.basename(python).lower() == "pythonw.exe":
        python = os.path.join(os.path.dirname(python), "python.exe")
    return [python, PROXY, "--handshake", HANDSHAKE_PATH, "--", *command]


def write_config():
    """Writes the Playwright MCP config file that adds AGENT_HEADER to every request of the browser (navigations,
    redirects, subresources; measured: no CORS preflight is added) and returns its path."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    config = {"browser": {"contextOptions": {"extraHTTPHeaders": {AGENT_HEADER: "1"}}}}
    atomic_io.write_secret_text(CONFIG_PATH, json.dumps(config, indent=1))
    return CONFIG_PATH


def probe(command, timeout=PROBE_TIMEOUT_S, handshake_path=None):
    """Starts the server over stdio, opens about:blank and closes it, as an agent's first call would.
    Returns None when the browser opened, else the reason it did not (a policy block, a crash, a timeout). With
    handshake_path, a successful probe also saves the server's initialize result and tool list there, the cache
    lazy_mcp_proxy answers from."""
    with tempfile.TemporaryDirectory(prefix="st-browser-probe-", ignore_cleanup_errors=True) as workdir:
        return _probe(command, workdir, timeout, handshake_path)


def _probe(command, workdir, timeout, handshake_path=None):
    """probe() inside workdir, the server's working folder."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(command, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                creationflags=flags)
    except OSError as exc:
        return f"server did not start: {exc}"
    lines, errors = queue.Queue(), []
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout] + [lines.put(None)], daemon=True).start()
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
                raise RuntimeError(f"server exited: {''.join(errors).strip()[-300:] or 'no output'}")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message") or message["error"])[:300])
                return message["result"]

    try:
        initialized = call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "smart-tool-probe", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        tools = call(4, "tools/list", {}).get("tools") if handshake_path else None
        result = call(2, "tools/call", {"name": "browser_navigate", "arguments": {"url": "about:blank"}})
        if result.get("isError"):
            text = " ".join(part.get("text", "") for part in result.get("content") or [])
            return f"browser did not open: {' '.join(text.split())[:300]}"
        call(3, "tools/call", {"name": "browser_close", "arguments": {}})
        if handshake_path:
            if not tools:
                return "the server listed no tools"
            os.makedirs(os.path.dirname(handshake_path), exist_ok=True)
            atomic_io.write_secret_text(handshake_path, json.dumps({"initialize": initialized, "tools": tools}))
        return None
    except (OSError, RuntimeError, TimeoutError) as exc:
        return str(exc)
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=flags)
        else:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def registered():
    """Whether each client already has a `playwright` MCP server (registered by Smart Tool or by the user)."""
    found = {"claude": False, "codex": False}
    try:
        with open(endpoint_sync.claude_json(), encoding="utf-8") as stream:
            found["claude"] = SERVER in (json.load(stream).get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(_codex_config(), encoding="utf-8") as stream:
            found["codex"] = f"[mcp_servers.{SERVER}]" in stream.read()
    except OSError:
        pass
    return found


def registered_executables(client):
    """Executables the client's `playwright` server needs: the registered command and, behind lazy_mcp_proxy, the real
    server's too; an empty list when it is not registered."""
    try:
        if client == "claude":
            with open(endpoint_sync.claude_json(), encoding="utf-8") as stream:
                entry = (json.load(stream).get("mcpServers") or {}).get(SERVER)
        else:
            with open(_codex_config(), encoding="utf-8") as stream:
                entry = (tomllib.loads(stream.read()).get("mcp_servers") or {}).get(SERVER)
    except (OSError, ValueError, AttributeError):
        return []
    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str):
        return []
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    real = args[args.index("--") + 1] if "--" in args and args.index("--") + 1 < len(args) else None
    return [command] + ([real] if isinstance(real, str) else [])


def _lazy_registration_usable(client):
    """Whether the client's registration goes through lazy_mcp_proxy and the handshake cache it answers from is
    readable: a registration of an older version (the Node server directly) or a lost cache both need ensure."""
    try:
        if client == "claude":
            with open(endpoint_sync.claude_json(), encoding="utf-8") as stream:
                entry = (json.load(stream).get("mcpServers") or {}).get(SERVER)
        else:
            with open(_codex_config(), encoding="utf-8") as stream:
                entry = (tomllib.loads(stream.read()).get("mcp_servers") or {}).get(SERVER)
        args = entry.get("args") or []
        if PROXY not in args:
            return False
        lazy_mcp_proxy.load_handshake(HANDSHAKE_PATH)
    except (OSError, ValueError, AttributeError, RuntimeError):
        return False
    return True


def heal(log=None):
    """Registers again, through ensure, each registration of ours whose executable no longer exists: the Node path is
    written into the client's configuration, which a moved or reinstalled Node leaves pointing nowhere. Returns
    {client: ensure's outcome} for the clients refreshed."""
    refreshed = {}
    for client in ("claude", "codex"):
        executables = registered_executables(client)
        ours = status().get(client, {}).get("state") in ("registered", "undenied")
        if ours and executables and not (all(node_locator.exists(path) for path in executables)
                                         and _lazy_registration_usable(client)):
            refreshed[client] = ensure(client, log)
    return refreshed


def _codex_config():
    """Codex keeps config.toml in CODEX_HOME when set, else ~/.codex."""
    return os.path.join(endpoint_sync.codex_dir(), "config.toml")


def _replace_text(path, text):
    temporary = f"{path}.smart-tool-tmp"
    with open(temporary, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)
    os.replace(temporary, path)


def set_claude_deny(present):
    """Adds (present=True) or removes the `permissions.deny` rules for DENIED_TOOLS in Claude Code's user settings,
    keeping every other rule. Raises ValueError when settings.json does not have the documented layout."""
    path = os.path.join(endpoint_sync.claude_dir(), "settings.json")
    try:
        with open(path, encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        data = {}
    permissions = data.get("permissions", {}) if isinstance(data, dict) else None
    deny = permissions.get("deny", []) if isinstance(permissions, dict) else None
    if not isinstance(deny, list):
        raise ValueError(f"{path} has no permissions.deny list Claude Code can read; fix it to deny "
                         f"{', '.join(DENIED_TOOLS)}.")
    rules = [f"mcp__{SERVER}__{tool}" for tool in DENIED_TOOLS]
    kept = [rule for rule in deny if rule not in rules]
    new = kept + rules if present else kept
    if new == deny:
        return
    permissions["deny"] = new
    if not new:
        del permissions["deny"]
    data["permissions"] = permissions
    if not permissions:
        del data["permissions"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _replace_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def set_codex_disabled():
    """Adds `disabled_tools` = DENIED_TOOLS to the [mcp_servers.playwright] table `codex mcp add` wrote (the CLI has
    no flag for it) and checks the file still parses with that value. Raises ValueError otherwise."""
    path = _codex_config()
    with open(path, encoding="utf-8", newline="") as stream:
        text = stream.read()

    def disabled(source):
        return ((tomllib.loads(source).get("mcp_servers") or {}).get(SERVER) or {}).get("disabled_tools")
    current = disabled(text)
    if current is not None:
        if set(DENIED_TOOLS) <= set(current):
            return
        raise ValueError(f"{path} already sets other disabled_tools for {SERVER}; add {', '.join(DENIED_TOOLS)}.")
    match = _CODEX_TABLE_RE.search(text)
    if not match:
        raise ValueError(f"[mcp_servers.{SERVER}] not found in {path}.")
    newline = "\r\n" if match.group(0).endswith("\r\n") else "\n"
    new = (text[:match.end()] + "disabled_tools = [" + ", ".join(json.dumps(t) for t in DENIED_TOOLS) + "]"
           + newline + text[match.end():])
    if not set(DENIED_TOOLS) <= set(disabled(new) or []):
        raise ValueError(f"disabled_tools did not take effect in {path}.")
    _replace_text(path, new)


def deny_unsafe_tools(client):
    """Denies DENIED_TOOLS in the client where Smart Tool registered the server."""
    if client == "claude":
        set_claude_deny(True)
    elif client == "codex":
        set_codex_disabled()
    else:
        raise ValueError(f"Client without MCP registration: {client!r}")


def register_command(client, command):
    """Official CLI command that registers a stdio server (Claude Code: user scope, every project)."""
    if client == "claude":
        return ["claude", "mcp", "add", "--scope", "user", SERVER, "--", *command]
    if client == "codex":
        return ["codex", "mcp", "add", SERVER, "--", *command]
    raise ValueError(f"Client without MCP registration: {client!r}")


def remove_command(client):
    """Official CLI command that removes the server registered by register_command."""
    if client == "claude":
        return ["claude", "mcp", "remove", SERVER, "--scope", "user"]
    if client == "codex":
        return ["codex", "mcp", "remove", SERVER]
    raise ValueError(f"Client without MCP registration: {client!r}")


def status():
    """Last outcome per client: {"claude": {"state": "registered"|"unavailable"|"kept"|"undenied", "reason", "at"}}."""
    try:
        with open(STATUS_PATH, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{STATUS_PATH} is not a browser control status file: fix or delete it.")
    return data


def _record(client, state, reason="", log=None):
    if log and reason:
        log(f"browser control {state} for {client}: {reason}")
    data = status()
    data[client] = {"state": state, "reason": reason, "at": time.time()}
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    atomic_io.write_secret_text(STATUS_PATH, json.dumps(data, indent=1, ensure_ascii=False))
    return data[client]


def _clip(text, limit=300):
    """text within limit characters, keeping its start and its end: a failed CLI call names the long command first and
    the CLI's own message last."""
    return text if len(text) <= limit else text[:limit // 3] + " ... " + text[-(limit - limit // 3 - 5):]


def ensure(client, log=None):
    """Registers Playwright MCP in the client, or refreshes the registration Smart Tool made before (Node, Chromium
    and install paths change between versions). A `playwright` server the user registered is kept. When the browser
    cannot be driven on this machine (probe failed, CLI refused) records `unavailable` with the reason, removes a
    registration of ours that would no longer work and returns: agents keep the read-only browsers of web_fetch and
    web_search. A registration whose DENIED_TOOLS cannot be denied in the client is removed too; when even that fails
    the state is `undenied` (the next call retries it as ours). Only an unreadable status file raises (ValueError).
    log (optional) also gets the reason."""
    ours = status().get(client, {}).get("state") in ("registered", "undenied")
    if registered().get(client) and not ours:
        return _record(client, "kept", "a playwright MCP server you registered is kept")
    try:
        command = server_command()
        problem = probe(command, handshake_path=HANDSHAKE_PATH)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        problem = str(exc)
    try:
        if ours and registered().get(client):
            endpoint_sync.run_cli(remove_command(client))
        if problem:
            if ours and client == "claude":
                set_claude_deny(False)
            return _record(client, "unavailable", problem[:300], log)
        endpoint_sync.run_cli(register_command(client, lazy_command(command)))
        if not registered().get(client):
            return _record(client, "unavailable", "the client CLI finished, but the server did not appear in its "
                           "configuration", log)
        try:
            deny_unsafe_tools(client)
        except (OSError, ValueError) as exc:
            problem = f"{', '.join(DENIED_TOOLS)} could not be denied ({exc})"
            try:
                endpoint_sync.run_cli(remove_command(client))
            except RuntimeError as removal:
                return _record(client, "undenied", f"remove it with `{' '.join(remove_command(client))}`: {problem} "
                               f"and removing it failed ({removal})"[:300], log)
            return _record(client, "unavailable", f"{problem}, so the server was removed"[:300], log)
    except (OSError, RuntimeError, ValueError) as exc:
        # Our registration still in place after a failure (its removal failed) stays ours, flagged, never "kept".
        state = "undenied" if ours and registered().get(client) else "unavailable"
        return _record(client, state, _clip(str(exc)), log)
    return _record(client, "registered")
