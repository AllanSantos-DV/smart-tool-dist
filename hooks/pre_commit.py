#!/usr/bin/env python3
"""pre-commit hook of git (opt-in, written by `project_manage action=commit_hook_install`): asks the Smart Tool daemon
(POST /hooks/commit-check) to review the staged change and prints the notes to stderr. It never blocks a commit: the
exit status is always 0, a daemon that is down is one plain line, and a check that fails is printed, never silent.
The daemon address comes from its run file on every call (`hook_call`), never from a port written into the hook.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hook_call

DOWN = "Smart Tool is not running, commit not reviewed"
FAILED = "Smart Tool commit check failed: "


def run(root):
    """The text the hook prints for the repository at `root`: the notes of the daemon's review ("" when it found
    nothing), the one line of a daemon that is down, or the reason the check could not run."""
    output, failure = hook_call.call("commit-check", json.dumps({"root": root}).encode(), window_s=0)
    if output.get("ok") is True:
        notes = output.get("notes")
        return f"Smart Tool reviewed the staged change; the commit goes ahead.\n{notes}" if notes else ""
    if output.get("ok") is False:
        return FAILED + str(output.get("error"))
    if failure is None or "not running" in failure:
        return DOWN
    return FAILED + failure


def main():
    """Prints what `run` says about the repository the hook runs in (git starts it at the top of the work tree); any
    failure is printed and the exit status stays 0."""
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        text = run(os.getcwd())
    except Exception as exc:
        text = f"{FAILED}{type(exc).__name__}: {exc}"
    if text:
        print(text, file=sys.stderr)


if __name__ == "__main__":
    main()
