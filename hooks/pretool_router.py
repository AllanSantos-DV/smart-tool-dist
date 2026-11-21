#!/usr/bin/env python3
"""Command hook of Claude Code and Codex: thin client that forwards the payload to the Smart Tool daemon
(`--route pretool|posttool|subagent-start`), where the decision runs already warm (`hook_decision.py`). The daemon
address comes from its run file on every call, a restart is waited out with backoff (`hook_call`), and a daemon that
stays unreachable fails open: the call runs, neutral output and one plain message the first time.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hook_call

EVENTS = {"pretool": "PreToolUse", "posttool": "PostToolUse", "subagent-start": "SubagentStart"}


def _arg(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else ""


def run(argv, raw):
    """The JSON object the hook prints for the payload `raw` (bytes) under the command line `argv`."""
    route = _arg(argv, "--route")
    if route not in EVENTS:
        return {"systemMessage": hook_call.message(
            "command", "this hook entry is from an older Smart Tool version (no --route)",
            "The call ran without Smart Tool routing.", "Install the hooks again from the Smart Tool setup screen.")}
    output, failure = hook_call.call(route, raw, _arg(argv, "--client"))
    if failure is None:
        return output
    tool = ""
    try:
        tool = json.loads(raw or b"{}").get("tool_name") or ""
    except ValueError:
        pass
    effect = f"The {tool + ' ' if tool else ''}call was let through without Smart Tool routing."
    return {"systemMessage": hook_call.message(EVENTS[route], failure, effect)}


def main():
    """Reads the payload from stdin and prints the hook's JSON answer; any failure prints a message and lets the call run."""
    sys.stdout.reconfigure(encoding="utf-8")
    raw = sys.stdin.buffer.read()
    try:
        output = run(sys.argv[1:], raw)
    except Exception as exc:
        output = {"systemMessage": hook_call.message(
            "command", f"the hook failed ({type(exc).__name__}: {exc})", "The call was let through.")}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
