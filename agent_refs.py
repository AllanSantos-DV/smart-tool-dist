"""Stale references in the agent instruction files every session reads: for each tracked `CLAUDE.md` and `AGENTS.md`, at
the root and in folders, the paths, links and functions it writes that no longer exist. It is `doc_refs` pointed at these
files, so paths in a nested file resolve from its own folder. Nothing is written.
"""
import posixpath

import doc_refs

NAMES = ("CLAUDE.MD", "AGENTS.MD")
NOTE = ("Read from the git-tracked CLAUDE.md and AGENTS.md files, at the root and in folders: paths and functions in inline "
        "code, outside fenced blocks, and relative links. A path counts only when its extension exists in the project or "
        "its first folder does; a bare file name is found anywhere in the project; a function name that appears nowhere in "
        "the code is stale, so a name invented as an example is listed too and a function that is still called somewhere "
        "is not.")


def _wanted(rel):
    return posixpath.basename(rel).upper() in NAMES


def check(root, limit=doc_refs.DEFAULT_LIMIT):
    """The paths, links and functions the agent instruction files write that no longer exist.

    Parameters: root (str) project folder, a git repository; limit (int) stale references listed.
    Returns: dict shaped like `doc_refs.check` with "agents_checked" and "agents_unreadable" in "counts" in place of the
    doc counts; the summary says so when the project tracks no CLAUDE.md or AGENTS.md.
    Raises: ValueError when git cannot list the folder; RuntimeError or OSError when the code analyzers cannot run.
    """
    answer = doc_refs.scan(root, limit, "agent", (".md",), _wanted, NOTE)
    if not answer["counts"]["agents_checked"] and not answer["counts"]["agents_unreadable"]:
        answer["summary"] = "No CLAUDE.md or AGENTS.md is tracked in the project."
    return answer
