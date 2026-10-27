#!/usr/bin/env python3
"""SessionStart hook of Claude Code and Codex (opt-in, from the setup screen): forwards the payload to the Smart Tool daemon
(POST /hooks/session-audit) and prints its answer, the stored setup audit notice of the session folder as additional
context. Never starts the daemon or an audit: daemon down or any error means empty output, so a session start is
never delayed or blocked.
"""
import json
import os
import sys
import urllib.request

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import daemon_launcher

TIMEOUT_S = 5


def main():
    """Posts the SessionStart payload read from stdin to the running daemon and prints its JSON answer if not empty."""
    raw = sys.stdin.buffer.read()
    base = daemon_launcher.daemon_url_if_up()
    if not base:
        return
    request = urllib.request.Request(f"{base}/hooks/session-audit", data=raw,
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        output = json.loads(response.read() or b"{}")
    if output:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
