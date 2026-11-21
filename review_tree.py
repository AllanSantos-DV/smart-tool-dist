"""Review of the uncommitted working tree: the review `commit_check` gives the staged change, over the modified tracked files
as they are on disk, so what is not staged yet (an IDE, hand edits, another agent) is reviewed before it is staged. The text
before is the one at HEAD and the notes are those of `hook_decision.review_diff`. Nothing is written.
"""
import os

import affected_tests
import commit_check
import config
import hook_decision

CLEAN = "The working tree is clean: nothing to review."
NOTE = ("Reviewed: tracked files changed since HEAD, staged or not, read from disk. Untracked files, deleted files and files "
        "that are binary, not UTF-8 or over the review size are not reviewed.")


def _modified(root):
    """Paths relative to `root` of the tracked files changed since HEAD that still exist, sorted, and how many entries git
    lists in all (untracked and deleted files included).

    Raises: ValueError when git cannot read the repository or there is no HEAD commit.
    """
    try:
        changes = affected_tests._changes(root, "HEAD")
    except ValueError as exc:
        raise ValueError(f"The working tree cannot be reviewed against HEAD (not a git repository, or no commit yet): "
                         f"{exc}") from exc
    return sorted(rel for rel, found in changes.items() if found["status"] == "changed"), len(changes)


def review(root):
    """Reviews the modified tracked files of the repository at `root` with the notes the edit hook gives a finished diff.

    Parameters: root (str) project folder, inside a git repository with at least one commit.
    Returns: dict {"files": [reviewed paths], "notes": str (empty when every check is quiet; it says how many modified files
    past `commit_check.MAX_FILES` were not reviewed), "clean": bool (the tree has no change at all), "summary": str, "note":
    str (what the review covers)}.
    Raises: ValueError when git cannot read the repository or there is no HEAD commit, RuntimeError or OSError when a check
    cannot run.
    """
    paths, listed = _modified(root)
    if not listed:
        return {"files": [], "notes": "", "clean": True, "summary": CLEAN, "note": NOTE}
    changes = [change for change in hook_decision.file_changes(root, "HEAD", paths[:commit_check.MAX_FILES])
               if "\0" not in change[2]]
    notes = hook_decision.review_diff(changes, config.load_config()) if changes else ""
    left = len(paths) - commit_check.MAX_FILES
    if left > 0:
        skipped = (f"{left} modified file{'s were' if left != 1 else ' was'} not reviewed: the review reads the first "
                   f"{commit_check.MAX_FILES}.")
        notes = f"{notes} {skipped}".strip()
    files = [os.path.relpath(full, root).replace(os.sep, "/") for full, _before, _after in changes]
    summary = (f"Reviewed {len(files)} of {len(paths)} modified tracked file(s); {listed - len(paths)} other change(s) "
               "(untracked or deleted) are outside the review." if paths
               else f"No modified tracked file to review; {listed} other change(s) (untracked or deleted).")
    return {"files": files, "notes": notes, "clean": False, "summary": summary, "note": NOTE}
