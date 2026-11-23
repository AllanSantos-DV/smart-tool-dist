"""Credential shapes that git history added or removed, run only when asked and bounded by a commit limit and a time budget.

`git log -G` with a loose pattern of the shapes of `credential_check` picks the commits whose diffs may hold one, and the
exact shapes of `credential_check` decide on every added and removed line. A finding names the file, the line, the short
commit hash and the kind of the credential, never the value. Only the history reachable from HEAD under the project folder
is read; nothing is written.
"""
import re
import subprocess
import threading
import time

import credential_check

DEFAULT_LIMIT = 30
DEFAULT_COMMITS = 500
DEFAULT_BUDGET_SECONDS = 20
PREFILTER = (r"-----BEGIN|AKIA|ASIA[0-9A-Z]{16}|aws_secret_access_key|gh[pousr]_[A-Za-z0-9]{36}|github_pat_|"
             r"xox[abprs]-|AIza|[sr]k_live_|sk-ant-|npm_[A-Za-z0-9]{36}")
_PREFILTER = re.compile(PREFILTER, re.I)
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NOTE = ("Only the history reachable from HEAD under this folder is read, newest commits first; a credential that was "
        "added and removed again shows as two findings. Rotate any credential listed: removing it from the latest "
        "files does not remove it from history.")


def _git(root, *args):
    """Standard output of a git command run in `root`; ValueError when git cannot run or fails."""
    try:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"git {args[0]} could not run in {root}: {exc}") from exc
    if done.returncode:
        raise ValueError(f"git {args[0]} failed in {root}: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return done.stdout.decode("utf-8", "replace")


def _stream(root, hashes, budget_seconds):
    """Lines of `git log -p` over exactly these commits and whether the time budget cut the read short."""
    command = ["git", "-c", "core.quotepath=off", "log", "--no-walk=unsorted", "--stdin", "-p", "-U0", "--no-color",
               "--no-ext-diff", "--no-textconv", "--no-renames", "--relative", "--src-prefix=a/", "--dst-prefix=b/",
               "--format=%x01%h%x01%cI", "-i", "-G" + PREFILTER, "--", "."]
    proc = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=_NO_WINDOW)
    proc.stdin.write("\n".join(hashes).encode("ascii") + b"\n")
    proc.stdin.close()
    cut = threading.Event()

    def stop():
        cut.set()
        proc.kill()

    timer = threading.Timer(budget_seconds, stop)
    timer.daemon = True
    timer.start()
    lines = []
    try:
        for raw in proc.stdout:
            lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
    finally:
        timer.cancel()
        proc.stdout.close()
        proc.wait()
    return lines, cut.is_set()


def _findings(lines):
    """Credential findings of the added and removed lines of a `git log -p -U0` stream, newest commit first."""
    found, commit, date, path, old, new, header = [], None, None, None, 0, 0, False
    for text in lines:
        if text.startswith("\x01"):
            commit, date = text[1:].split("\x01")[:2]
            path, header = None, False
        elif text.startswith("diff --git "):
            path, header = None, True
        elif header and text.startswith("--- "):
            path = None if text == "--- /dev/null" else text[6:].split("\t")[0]
        elif header and text.startswith("+++ "):
            path = text[6:].split("\t")[0] if text != "+++ /dev/null" else path
        elif text.startswith("@@"):
            header = False
            hunk = _HUNK.match(text)
            if hunk:
                old, new = int(hunk.group(1)), int(hunk.group(2))
        elif not header and text[:1] in ("+", "-") and path and commit:
            added = text[0] == "+"
            hit = credential_check.review(None, text[1:]) if _PREFILTER.search(text) else None
            if hit:
                found.append({"commit": commit, "date": date, "path": path, "line": new if added else old,
                              "kind": hit[0]["kind"], "change": "added" if added else "removed"})
            if added:
                new += 1
            else:
                old += 1
    return found


def scan(root, limit=DEFAULT_LIMIT, commit_limit=DEFAULT_COMMITS, budget_seconds=DEFAULT_BUDGET_SECONDS):
    """Credential shapes added or removed by the newest commits, with file, line and commit, never the value.

    Parameters: root (str) project folder; limit (int) findings returned; commit_limit (int) newest commits read;
    budget_seconds (int or float) time the diff read may take before it is cut.
    Returns: dict {"findings": [{"commit" (short hash), "date", "path", "line", "kind", "change" ("added" or "removed")}]
    newest commit first, "total": int, "by_kind": {kind: count}, "commits_scanned": int, "commit_limit": int,
    "history_cut": bool (older commits exist beyond the limit), "complete": bool (false when the time budget cut the
    read), "budget_seconds": int or float, "elapsed_seconds": float, "note": str}.
    Raises: ValueError when the folder is not in a git repository or git fails.
    """
    started = time.monotonic()
    try:
        _git(root, "rev-parse", "--is-inside-work-tree")
    except ValueError as exc:
        raise ValueError(f"{root} is not a git repository: history_secrets reads git history. ({exc})") from exc
    answer = {"findings": [], "total": 0, "by_kind": {}, "commits_scanned": 0, "commit_limit": commit_limit,
              "history_cut": False, "complete": True, "budget_seconds": budget_seconds, "elapsed_seconds": 0.0,
              "note": NOTE}
    try:
        _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    except ValueError:
        answer["note"] = "The repository has no commits yet: nothing to scan."
        return answer
    hashes = _git(root, "rev-list", f"--max-count={commit_limit + 1}", "HEAD", "--", ".").split()
    answer["history_cut"] = len(hashes) > commit_limit
    hashes = hashes[:commit_limit]
    answer["commits_scanned"] = len(hashes)
    lines, cut = _stream(root, hashes, budget_seconds)
    found = _findings(lines)
    for item in found:
        answer["by_kind"][item["kind"]] = answer["by_kind"].get(item["kind"], 0) + 1
    answer.update(findings=found[:limit], total=len(found), complete=not cut,
                  elapsed_seconds=round(time.monotonic() - started, 2))
    if cut:
        answer["note"] = (f"Stopped at the time budget of {budget_seconds} s: later commits in the order newest first "
                          "were not read, so the findings are a lower bound. " + NOTE)
    elif answer["history_cut"]:
        answer["note"] = (f"Only the newest {commit_limit} commits were read; older commits were not. " + NOTE)
    return answer
