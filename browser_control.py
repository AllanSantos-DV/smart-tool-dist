"""Browser control for agents: the official Playwright MCP server, registered by default next to smart-tool.

Smart Tool's own browsers only read pages (web_fetch, web_search). Agents that must click, type or log in get
Microsoft's Playwright MCP (pinned in web_adapters/node), driving the Chromium Smart Tool already installs, in an
isolated in-memory profile, headless. Before registering, a probe launches that browser through the server exactly
as the agent would: machines whose policy forbids it (corporate IT blocking remote debugging, a missing browser)
keep today's read-only behavior, and the reason is shown in the setup screen instead of an error. A server named
`playwright` the user already registered is left untouched.
"""
import json
import os
import queue
import subprocess
import tempfile
import threading
import time

import atomic_io
import endpoint_sync
import install_runtime
import paths

SERVER = "playwright"
CLI = install_runtime.PLAYWRIGHT_MCP_CLI
STATUS_PATH = os.path.join(paths.DATA_DIR, "browser-control.json")
OUTPUT_DIR = os.path.join(paths.DATA_DIR, "playwright-mcp")
OUTPUT_MAX_BYTES = 100 * 1024 * 1024
PROBE_TIMEOUT_S = 60
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
    return [node, str(CLI), "--isolated", "--headless", "--executable-path", chromium_executable(),
            "--output-dir", OUTPUT_DIR, "--output-max-size", str(OUTPUT_MAX_BYTES)]


def probe(command, timeout=PROBE_TIMEOUT_S):
    """Starts the server over stdio, opens about:blank and closes it, as an agent's first call would.
    Returns None when the browser opened, else the reason it did not (a policy block, a crash, a timeout)."""
    with tempfile.TemporaryDirectory(prefix="st-browser-probe-", ignore_cleanup_errors=True) as workdir:
        return _probe(command, workdir, timeout)


def _probe(command, workdir, timeout):
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
        call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "smart-tool-probe", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        result = call(2, "tools/call", {"name": "browser_navigate", "arguments": {"url": "about:blank"}})
        if result.get("isError"):
            text = " ".join(part.get("text", "") for part in result.get("content") or [])
            return f"browser did not open: {' '.join(text.split())[:300]}"
        call(3, "tools/call", {"name": "browser_close", "arguments": {}})
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
        with open(os.path.join(os.path.expanduser("~"), ".codex", "config.toml"), encoding="utf-8") as stream:
            found["codex"] = f"[mcp_servers.{SERVER}]" in stream.read()
    except OSError:
        pass
    return found


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
    """Last outcome per client: {"claude": {"state": "registered"|"unavailable"|"kept", "reason", "at"}, ...}."""
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


def ensure(client, log=None):
    """Registers Playwright MCP in the client, or refreshes the registration Smart Tool made before (Node, Chromium
    and install paths change between versions). A `playwright` server the user registered is kept. When the browser
    cannot be driven on this machine (probe failed, CLI refused) records `unavailable` with the reason, removes a
    registration of ours that would no longer work and returns: agents keep the read-only browsers of web_fetch and
    web_search. Only an unreadable status file raises (ValueError). log (optional) also gets the reason."""
    ours = status().get(client, {}).get("state") == "registered"
    if registered().get(client) and not ours:
        return _record(client, "kept", "a playwright MCP server you registered is kept")
    try:
        command = server_command()
        problem = probe(command)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        problem = str(exc)
    try:
        if ours and registered().get(client):
            endpoint_sync.run_cli(remove_command(client))
        if problem:
            return _record(client, "unavailable", problem[:300], log)
        endpoint_sync.run_cli(register_command(client, command))
    except RuntimeError as exc:
        return _record(client, "unavailable", str(exc)[:300], log)
    if not registered().get(client):
        return _record(client, "unavailable", "the client CLI finished, but the server did not appear in its configuration", log)
    return _record(client, "registered")
