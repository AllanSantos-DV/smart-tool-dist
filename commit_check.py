"""Commit-time review of the staged change, and the opt-in git hooks that print reviews.

The review is the one the edit hook gives a finished diff (`hook_decision.review_diff`), over what `git diff --cached`
holds, so edits the agent hook never saw (an IDE, hand edits, another agent) are reviewed before they reach the commit.
The `pre-commit` script this module writes only forwards to the daemon (`hooks/pre_commit.py`) and never blocks a commit;
the `pre-push` script does the same for the commits a push sends (`hooks/pre_push.py`, `push_check.py`).
"""
import glob
import json
import os
import shlex
import subprocess
import sys

import affected_tests
import atomic_io
import client_hooks
import config
import hook_decision

MARKER = "# smart-tool-commit-check"
HOOK_NAME = "pre-commit"
HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "pre_commit.py")
HOOK_ORIGIN = client_hooks.HOOK_ORIGIN
PUSH_MARKER = "# smart-tool-pre-push"
PUSH_HOOK_NAME = "pre-push"
PUSH_HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "pre_push.py")
DEFAULT_HOOK = HOOK_NAME
MAX_FILES = 50
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git_raw(root, *args):
    """The finished `git` process (bytes output) run in `root`; the caller reads the exit code."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=60, creationflags=NO_WINDOW)


def _staged_text(root, rel):
    """The text of `rel` as staged, or None when it is binary, not UTF-8 or over the review size."""
    done = _git_raw(root, "show", f":./{rel}")
    if done.returncode or len(done.stdout) > hook_decision.MAX_REVIEW_BYTES:
        return None
    try:
        text = done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\0" in text else text


def _staged_paths(root):
    """Paths relative to `root` of the files the commit adds or changes (a deletion has nothing to review), sorted."""
    fields = affected_tests._git(root, "diff", "--cached", "--name-status", "-z", "--no-renames", "--relative",
                                 "--", ".").split("\0")
    return sorted(path for status, path in zip(fields[0::2], fields[1::2]) if status != "D")


def review(root):
    """Reviews the staged change of the repository at `root` with the notes the edit hook gives a finished diff.

    Parameters: root (str) project folder, inside a git repository.
    Returns: dict with `files` (the staged files that were reviewed) and `notes` (str; empty when every check is quiet,
    and it says how many staged files past MAX_FILES were not reviewed).
    Raises: ValueError when git cannot read the index, RuntimeError or OSError when a check cannot run.
    """
    paths = _staged_paths(root)
    changes = []
    for rel in paths[:MAX_FILES]:
        after = _staged_text(root, rel)
        if after is None:
            continue
        try:
            before = affected_tests._git(root, "show", f"HEAD:./{rel}")
        except ValueError:
            before = None
        changes.append((rel, os.path.join(root, *rel.split("/")), before, after))
    notes = hook_decision.review_diff([(full, before, after) for _rel, full, before, after in changes],
                                      config.load_config()) if changes else ""
    left = len(paths) - MAX_FILES
    if left > 0:
        skipped = f"{left} staged file{'s were' if left != 1 else ' was'} not reviewed: the review reads the first {MAX_FILES}."
        notes = f"{notes} {skipped}".strip()
    return {"files": [rel for rel, *_ in changes], "notes": notes}


def hook_python():
    """The interpreter the hook runs, with forward slashes and no spaces: this process's own, or its console twin when
    that is the windowless `pythonw`, which would drop the notes the hook prints to stderr."""
    path = sys.executable
    if os.path.basename(path).lower() == "pythonw.exe":
        path = os.path.join(os.path.dirname(path), "python.exe")
    return client_hooks._shell_safe(path)


def _hook_text():
    """The `pre-commit` script: the review client of the product, and a visible line, never a failing exit, when it
    cannot start (a git hook that exits non-zero blocks the commit)."""
    command = f"{shlex.quote(hook_python())} {shlex.quote(client_hooks._shell_safe(HOOK_SCRIPT))} {HOOK_ORIGIN}"
    return (f"#!/bin/sh\n{MARKER}: written by Smart Tool (project_manage action=commit_hook_install); "
            f"commit_hook_uninstall removes it\n"
            f'{command} || echo "Smart Tool commit check could not run (exit $?), commit not reviewed" >&2\n'
            "exit 0\n")


def _push_hook_text():
    """The `pre-push` script: it passes git's arguments and, through the inherited stdin, the pushed ranges to the review
    client; the command line ends with the hook origin, the failure line is printed, and the exit status stays 0 (a git
    hook that exits non-zero blocks the push)."""
    command = f"{shlex.quote(hook_python())} {shlex.quote(client_hooks._shell_safe(PUSH_HOOK_SCRIPT))}"
    return (f"#!/bin/sh\n{PUSH_MARKER}: written by Smart Tool (project_manage action=commit_hook_install hook=pre-push); "
            f"commit_hook_uninstall hook=pre-push removes it\n"
            f'{command} "$@" {HOOK_ORIGIN}\n'
            "status=$?\n"
            '[ "$status" -eq 0 ] || echo "Smart Tool push review could not run (exit $status), push not reviewed" >&2\n'
            "exit 0\n")


_HOOKS = {
    HOOK_NAME: {"marker": MARKER, "text": _hook_text, "label": "commit",
                "note": "Commits in this repository print the review of the staged change to stderr and never wait on it "
                        "to succeed."},
    PUSH_HOOK_NAME: {"marker": PUSH_MARKER, "text": _push_hook_text, "label": PUSH_HOOK_NAME,
                     "note": "Pushes from this repository print the tests affected by the pushed commits and the impact "
                             "summary to stderr and never wait on it to succeed."},
}


def _spec(hook):
    """The entry of `_HOOKS` for the git hook named `hook`; raises ValueError naming the accepted hooks."""
    if hook not in _HOOKS:
        raise ValueError(f"hook must be one of {', '.join(_HOOKS)}, not {hook!r}.")
    return _HOOKS[hook]


def _hooks_dir(root):
    """The absolute folder git runs hooks from for the repository at `root` (a worktree or submodule shares the one
    its `.git` file points to)."""
    return os.path.abspath(os.path.join(root, affected_tests._git(root, "rev-parse", "--git-path", "hooks").strip()))


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as stream:
        return stream.read()


def _husky(root):
    """Sentences naming where husky manages the git hooks of the repository: a `.husky` folder, or `husky` in the
    dependencies or scripts of a package.json, in `root` and in the top of the repository."""
    top = affected_tests._git(root, "rev-parse", "--show-toplevel").strip()
    found = []
    for folder in {os.path.realpath(path): path for path in (root, top)}.values():
        if os.path.isdir(os.path.join(folder, ".husky")):
            found.append(f"husky manages the git hooks here (a .husky folder in {folder})")
        try:
            package = json.loads(_read(os.path.join(folder, "package.json")))
        except (OSError, ValueError):
            continue
        used = isinstance(package, dict) and (
            any("husky" in (package.get(kind) or {}) for kind in ("dependencies", "devDependencies")) or
            any("husky" in str(command) for command in (package.get("scripts") or {}).values()))
        if used:
            found.append(f"husky manages the git hooks here (package.json in {folder} names it)")
    return found


def _blockers(root, hook, marker=MARKER):
    """Sentences naming what makes the install refuse: `core.hooksPath` set, husky, or a hook file at `hook` without our
    `marker` line."""
    found = []
    configured = _git_raw(root, "config", "--show-scope", "--get", "core.hooksPath")
    if configured.returncode == 0:
        scope, _, value = configured.stdout.decode("utf-8", "replace").strip().partition("\t")
        found.append(f"core.hooksPath is set ({scope}) to {value}, so git does not run .git/hooks")
    elif configured.returncode != 1:
        raise ValueError(f"git config core.hooksPath failed: {configured.stderr.decode('utf-8', 'replace').strip()[:300]}")
    found += _husky(root)
    if os.path.isfile(hook) and marker not in _read(hook):
        found.append(f"{hook} already exists and is not Smart Tool's (it has no {marker} line)")
    return found


def install_hook(root, hook=DEFAULT_HOOK):
    """Writes the opt-in git hook `hook` of the repository at `root`, after a backup of an older hook of ours.

    Parameters: root (str) project folder, inside a git repository; hook (str) `pre-commit` or `pre-push`.
    Returns: dict with `installed`, `changed`, `file` and `backup` (path of the copy of our older hook, None when new).
    Raises: ValueError, naming everything found and changing nothing, when `hook` is unknown, core.hooksPath is set,
    husky is used or a hook of that name that is not ours exists.
    """
    spec = _spec(hook)
    path = os.path.join(_hooks_dir(root), hook)
    found = _blockers(root, path, spec["marker"])
    if found:
        raise ValueError(f"The {spec['label']} hook was not installed and nothing was changed: " + "; ".join(found) + ".")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = atomic_io.backup_file(path)
    atomic_io.write_text(path, spec["text"]())
    os.chmod(path, 0o755)
    return {"installed": True, "changed": True, "file": path, "backup": backup, "note": spec["note"]}


def uninstall_hook(root, hook=DEFAULT_HOOK):
    """Removes our git hook `hook` of the repository at `root` and puts back a backed-up hook that is not ours.

    Parameters: root (str) project folder, inside a git repository; hook (str) `pre-commit` or `pre-push`.
    Returns: dict with `removed`, `restored` (path of the backup put back, None when there is none) and `file`; a
    repository without a hook answers `removed` false with a `note`.
    Raises: ValueError when `hook` is unknown or the hook file is not ours; it stays as it is.
    """
    marker = _spec(hook)["marker"]
    path = os.path.join(_hooks_dir(root), hook)
    if not os.path.isfile(path):
        return {"removed": False, "restored": None, "file": path, "note": f"No {hook} hook at {path}."}
    if marker not in _read(path):
        raise ValueError(f"{path} is not Smart Tool's (it has no {marker} line) and was left as it is.")
    os.remove(path)
    copies = sorted(glob.glob(glob.escape(path + atomic_io.BACKUP_INFIX) + "*"), reverse=True)
    saved = next((copy for copy in copies if marker not in _read(copy)), None)
    if saved:
        os.replace(saved, path)
    return {"removed": True, "restored": saved, "file": path}
