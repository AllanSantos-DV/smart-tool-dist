#!/usr/bin/env python3
"""Stdio MCP proxy that starts the real server only when a request needs it.

Claude Code and Codex start every registered stdio server with the session, so a Playwright MCP server (Node, about
97 MB of commit) sat idle in each session that never opened a browser. This proxy is what gets registered: it answers
`initialize`, `ping` and `tools/list` from the handshake cache browser_control wrote after probing the real server, and
starts the real command on the first other request (a tool call), replaying the client's `initialize` to it. From then
on every message passes through unchanged. When the client closes stdin the real server's process tree is killed.

Usage: lazy_mcp_proxy.py --handshake FILE -- COMMAND [ARG...]
The cache (`{"initialize": <result>, "tools": [...]}`) missing or unreadable is an error on stderr and exit code 2, never
an empty tool list.
"""
import json
import os
import queue
import subprocess
import sys
import threading

LAZY_ID = "smart-tool-lazy-init"
INIT_TIMEOUT_S = 60
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_handshake(path):
    """The cached initialize result and tool list; RuntimeError when the file cannot be read or has the wrong shape."""
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data["initialize"], dict) or not isinstance(data["tools"], list):
            raise TypeError("initialize must be an object and tools a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"handshake cache {path} is unusable ({type(exc).__name__}: {exc}); run browser_control.ensure "
                           f"again (reinstall Smart Tool).") from exc
    return data


class Proxy:
    """One client conversation: local answers until a request needs the real server, then a pass-through."""

    def __init__(self, handshake, command, stdin, stdout):
        self.handshake, self.command = handshake, command
        self.stdin, self.stdout = stdin, stdout
        self.child = None
        self.closing = False
        self.client_init = None
        self.write_lock = threading.Lock()

    def send(self, message):
        """Writes one JSON-RPC message (dict) or one raw line (bytes) to the client."""
        data = message if isinstance(message, bytes) else json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with self.write_lock:
            self.stdout.write(data)
            self.stdout.flush()

    def answer(self, request_id, result):
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def local_initialize(self, params):
        """The cached initialize result, speaking the protocol version the client asked for when it is a known one."""
        result = dict(self.handshake["initialize"])
        asked = (params or {}).get("protocolVersion")
        if asked in KNOWN_PROTOCOLS:
            result["protocolVersion"] = asked
        return result

    def start_child(self):
        """Starts the real server and replays the client's initialize to it; its answer is not forwarded (the client
        already got the cached one). RuntimeError when it exits or answers with an error."""
        child = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                                 creationflags=_NO_WINDOW)
        params = self.client_init or {"protocolVersion": KNOWN_PROTOCOLS[2], "capabilities": {},
                                      "clientInfo": {"name": "smart-tool-lazy-proxy", "version": "1"}}
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": LAZY_ID, "method": "initialize", "params": params}).encode()
                          + b"\n")
        child.stdin.flush()
        answer = self.read_initialize(child)
        if "error" in answer:
            self.kill(child)
            raise RuntimeError(f"the real server refused initialize: {answer['error']}")
        child.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        child.stdin.flush()
        self.child = child
        threading.Thread(target=self.pump_child, args=(child,), daemon=True).start()

    @classmethod
    def read_initialize(cls, child):
        """The real server's answer to the replayed initialize; RuntimeError (child killed) when it exits or stays silent
        for INIT_TIMEOUT_S."""
        answers = queue.Queue()

        def read():
            for line in child.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("id") == LAZY_ID:
                    answers.put(message)
                    return
            answers.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            answer = answers.get(timeout=INIT_TIMEOUT_S)
        except queue.Empty:
            cls.kill(child)
            raise RuntimeError(f"the real server did not answer initialize in {INIT_TIMEOUT_S}s") from None
        if answer is None:
            raise RuntimeError(f"the real server exited before answering initialize (code {child.wait(10)})")
        return answer

    def pump_child(self, child):
        """Child to client until the child closes its stdout; then the proxy exits with the child's code, loudly."""
        for line in child.stdout:
            self.send(line)
        code = child.wait()
        if self.closing:
            return
        print(f"lazy_mcp_proxy: the real server exited (code {code})", file=sys.stderr, flush=True)
        os._exit(code or 1)

    @staticmethod
    def kill(child):
        if child.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
        else:
            child.kill()

    def handle(self, line):
        """One client line: answered here while the real server is not needed, else passed to it."""
        try:
            message = json.loads(line)
        except ValueError:
            print(f"lazy_mcp_proxy: invalid line ignored: {line[:200]!r}", file=sys.stderr, flush=True)
            return
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            self.client_init = message.get("params")
        if self.child is None and request_id is not None and method == "initialize":
            return self.answer(request_id, self.local_initialize(message.get("params")))
        if self.child is None and request_id is not None and method == "ping":
            return self.answer(request_id, {})
        if self.child is None and request_id is not None and method == "tools/list":
            return self.answer(request_id, {"tools": self.handshake["tools"]})
        if self.child is None and (method is None or request_id is None):
            return  # notifications and responses mean nothing to a server that is not running
        if self.child is None:
            try:
                self.start_child()
            except (OSError, RuntimeError) as exc:
                self.send({"jsonrpc": "2.0", "id": request_id,
                           "error": {"code": -32000, "message": f"could not start the server: {exc}"[:500]}})
                return
        self.child.stdin.write(line if line.endswith(b"\n") else line + b"\n")
        self.child.stdin.flush()

    def run(self):
        for line in self.stdin:
            if line.strip():
                self.handle(line)
        self.closing = True
        if self.child is not None:
            self.kill(self.child)


def main(argv):
    if "--" not in argv or argv[:1] != ["--handshake"] or len(argv) < 4:
        print("usage: lazy_mcp_proxy.py --handshake FILE -- COMMAND [ARG...]", file=sys.stderr)
        return 2
    split = argv.index("--")
    try:
        handshake = load_handshake(argv[1])
    except RuntimeError as exc:
        print(f"lazy_mcp_proxy: {exc}", file=sys.stderr)
        return 2
    Proxy(handshake, argv[split + 1:], sys.stdin.buffer, sys.stdout.buffer).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
