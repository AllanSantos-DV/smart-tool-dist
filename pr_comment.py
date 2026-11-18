"""Posts the `pr_impact` report as a comment on a pull request through the gh CLI.

This writes to GitHub with the user's own gh login, so it runs only while the setup screen's `pr_comment` consent is on.
The comment carries a hidden marker: a rerun edits the last comment that has it instead of stacking a new one.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import config
import pr_impact

MARKER = "<!-- smart-tool:pr-impact -->"
GH_TIMEOUT_S = 60
COMMENT_ID = re.compile(r"#issuecomment-(\d+)$")
USER_FOLDER = re.compile(r"(?<![\w.\-/\\])(?:[A-Za-z]:)?[\\/](?:Users|home)[\\/][^\s`'\"|)\]]+")
DRIVE_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s`'\"|)\]]*")


def _gh(gh, root, *args):
    """Runs gh from the project folder and returns its stdout; a failure says what gh answered."""
    try:
        done = subprocess.run([gh, *args], cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=GH_TIMEOUT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gh {' '.join(args[:2])} could not run: {exc}")
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def _relativize(text, root):
    """Drops the project folder from every path in text and replaces any other user-folder or drive path, so the
    comment shows no location on the user's machine."""
    absolute = os.path.abspath(root)
    parts = [re.escape(part) for part in re.split(r"[\\/]+", absolute) if part]
    folder = (r"[\\/]" if absolute[:1] in "/\\" else "") + r"[\\/]+".join(parts)
    text = re.sub(folder + r"[\\/]+", "", text, flags=re.I)
    text = re.sub(folder + r"(?![\w.\-])", ".", text, flags=re.I)
    return DRIVE_PATH.sub("<local path>", USER_FOLDER.sub("<local path>", text))


def _previous_comment_id(gh, root, pr):
    """The id of the last comment on the pull request that carries the marker, or None."""
    comments = json.loads(_gh(gh, root, "pr", "view", str(pr), "--json", "comments"))["comments"]
    for comment in reversed(comments):
        found = COMMENT_ID.search(comment.get("url", ""))
        if MARKER in comment.get("body", "") and found:
            return found.group(1)
    return None


def publish(root, pr, base, limit=30, view_id=None):
    """Posts the impact report of the change against base on pull request pr, or replaces the previous Smart Tool comment.

    Parameters: root (str) project folder, where gh finds the repository; pr (int) pull request number; base (str) git
    revision the report compares with; limit (int) rows per list; view_id (str or None).
    Returns: dict with the comment 'url', whether it was 'updated' and the 'pr'.
    Raises: RuntimeError when the consent is off, gh is missing or not logged in, or gh fails; ValueError for a bad pr or base.
    """
    if config.pr_comment() != "on":
        raise RuntimeError("pr_comment is off: nothing is posted to GitHub. The user turns it on in the Smart Tool setup "
                           "screen (Agent rules, Impact report as a pull request comment).")
    if type(pr) is not int or pr < 1:
        raise ValueError("pr must be the number of the pull request, a positive integer.")
    if not isinstance(base, str) or not base:
        raise ValueError("base is required: the git revision the pull request is compared with, such as origin/main.")
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("The gh CLI is not installed or not on PATH: install it from https://cli.github.com and run gh auth login.")
    try:
        _gh(gh, root, "auth", "status")
    except RuntimeError as exc:
        raise RuntimeError(f"gh is not logged in ({exc}); run gh auth login.")
    body = _relativize(pr_impact.report(root, base, limit, view_id)["markdown"], root) + "\n" + MARKER + "\n"
    previous = _previous_comment_id(gh, root, pr)
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "comment.md")
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
        if previous:
            answer = json.loads(_gh(gh, root, "api", "--method", "PATCH",
                                    f"repos/{{owner}}/{{repo}}/issues/comments/{previous}", "-F", f"body=@{path}"))
            url = answer.get("html_url", "")
        else:
            lines = _gh(gh, root, "pr", "comment", str(pr), "--body-file", path).strip().splitlines()
            url = lines[-1] if lines else ""
    if not url.startswith("https://"):
        raise RuntimeError("gh did not return the comment URL, so the post is not confirmed; look at the pull request.")
    return {"url": url, "updated": bool(previous), "pr": pr}
