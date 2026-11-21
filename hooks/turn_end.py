#!/usr/bin/env python3
"""Stop hook of Claude Code (type command): forwards the payload to the Smart Tool daemon (POST /hooks/stop), whose
address it reads on every call from the run file the daemon writes (`hook_call`), and prints the daemon's answer (a block
with the docstrings to review, or nothing). A continuation (stop_hook_active) exits at once without calling the daemon.
A restart is waited out; a daemon that stays unreachable prints one plain message and the step stays pending.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hook_call


def run(raw):
    """The JSON object the hook prints for the Stop payload `raw` (bytes); {} for a continuation or an empty answer."""
    if json.loads(raw or b"{}").get("stop_hook_active"):
        return {}
    output, failure = hook_call.call("stop", raw)
    if failure is None:
        return output
    return {"systemMessage": hook_call.message(
        "Stop", failure, "The end-of-turn documentation step did not run and its work stays pending.")}


def main():
    """Reads the Stop payload from stdin and prints the answer when it is not empty; a failure prints a message."""
    sys.stdout.reconfigure(encoding="utf-8")
    raw = sys.stdin.buffer.read()
    try:
        output = run(raw)
    except Exception as exc:
        output = {"systemMessage": hook_call.message(
            "Stop", f"the hook failed ({type(exc).__name__}: {exc})", "The end-of-turn documentation step did not run.")}
    if output:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
