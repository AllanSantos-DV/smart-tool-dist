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
MAX_LISTED = 8


def _listed(label, lines, total):
    """`label` and the first MAX_LISTED of `lines`, with how many of the `total` are not shown."""
    shown = lines[:MAX_LISTED]
    more = [f"  ... and {total - len(shown)} more"] if total > len(shown) else []
    return [label] + [f"  {line}" for line in shown] + more


def _impact_lines(output):
    """The lines for the tests to run and the staged functions no test reaches; none when there is nothing to say, one
    line with the reason when the check could not run."""
    section = output.get("tests_to_run") or {}
    if section.get("checked") is False:
        return [f"Tests to run not checked: {section.get('problem')}."]
    lines = []
    if section.get("tests"):
        lines += _listed("Tests affected by the staged change:", [f"{t['path']}: {t['why']}" for t in section["tests"]],
                         section.get("total", len(section["tests"])))
        lines += [f"  {c['command']} (in {c['cwd']})" if c.get("command") else f"  {c['reason']} ({', '.join(c['files'])})"
                  for c in section.get("commands") or []]
    if section.get("run_all"):
        lines.append("  A changed file can change every test: run the whole suite.")
    untouched = output.get("untouched_by_tests") or []
    if untouched:
        lines += _listed("Touched functions no test reaches:", untouched, len(untouched))
    if section.get("note"):
        lines.append(section["note"])
    return lines


def run(root):
    """The text the hook prints for the repository at `root`: the notes of the daemon's review and the tests to run
    ("" when it found nothing), the one line of a daemon that is down, or the reason the check could not run."""
    output, failure = hook_call.call("commit-check", json.dumps({"root": root}).encode(), window_s=0)
    if output.get("ok") is True:
        lines = ([output["notes"]] if output.get("notes") else []) + _impact_lines(output)
        return "Smart Tool reviewed the staged change; the commit goes ahead.\n" + "\n".join(lines) if lines else ""
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
