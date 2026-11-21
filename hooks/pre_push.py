#!/usr/bin/env python3
"""pre-push hook of git (opt-in, written by `project_manage action=commit_hook_install hook=pre-push`): sends the pushed
ranges git writes to stdin to the Smart Tool daemon (POST /hooks/pre-push) and prints to stderr the tests the pushed
commits affect and the impact summary of the change. It never blocks a push: the exit status is always 0, a daemon that
is down is one plain line naming this hook's origin, and a review that fails is printed, never silent.
The daemon address comes from its run file on every call (`hook_call`), never from a port written into the hook.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hook_call

ORIGIN_FLAG = "--hook-origin="
DOCS_FLAG = "--docs"
DOWN = f"[hook-origin={hook_call.ORIGIN}] Smart Tool is not running, push not reviewed"
FAILED = f"[hook-origin={hook_call.ORIGIN}] Smart Tool push review failed: "


def pushed_ranges(text):
    """The ranges of git's stdin, one `<local ref> <local sha> <remote ref> <remote sha>` line each, as dicts with the
    keys `local_ref`, `local`, `remote_ref` and `remote`; raises ValueError on a line of another shape."""
    ranges = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise ValueError(f"git sent a pre-push line with {len(fields)} fields instead of 4: {line[:200]!r}")
        ranges.append(dict(zip(("local_ref", "local", "remote_ref", "remote"), fields)))
    return ranges


def run(root, remote, ranges, docs=False):
    """The text the hook prints for the push of `ranges` from the repository at `root` to `remote`: what the daemon's
    review says ("" when there is nothing to say), the one line of a daemon that is down, or the reason the review could
    not run. With `docs` the request asks for the documentation section; without it the request is the one it always was."""
    raw = json.dumps({"root": root, "remote": remote, "ranges": ranges, **({"docs": True} if docs else {})}).encode()
    output, failure = hook_call.call("pre-push", raw, window_s=0)
    if output.get("ok") is True:
        return str(output.get("text") or "")
    if output.get("ok") is False:
        return FAILED + str(output.get("error"))
    if failure is None or "not running" in failure:
        return DOWN
    return FAILED + failure


def main():
    """Prints what `run` says about the push the hook runs for (git starts it at the top of the work tree, passes the
    remote name and URL as arguments and the ranges on stdin; the `--docs` flag of the installed command asks for the
    documentation section); any failure is printed and the exit status stays 0."""
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        arguments = [arg for arg in sys.argv[1:] if not arg.startswith(ORIGIN_FLAG) and arg != DOCS_FLAG]
        ranges = pushed_ranges(sys.stdin.buffer.read().decode("utf-8", "replace"))
        text = run(os.getcwd(), arguments[0] if arguments else "", ranges,
                   DOCS_FLAG in sys.argv[1:]) if ranges else ""
    except Exception as exc:
        text = f"{FAILED}{type(exc).__name__}: {exc}"
    if text:
        print(text, file=sys.stderr)


if __name__ == "__main__":
    main()
