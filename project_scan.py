"""Whole-project scans of the files git tracks, read from disk: credentials committed earlier or sitting in a `.env`
file (the edit hook only sees the lines an edit writes, and the indexer skips secret files) and relative links of HTML
and Markdown files that point to nothing. Nothing is written and no value of a credential is returned.
"""
import os
import subprocess

import credential_check
import link_check

DEFAULT_LIMIT = 25
MAX_BYTES = 2_000_000


def tracked_files(root):
    """Project-relative paths of the files git tracks under `root` that exist on disk.

    Parameters: root (str) project folder.
    Returns: list of str with forward slashes, in git's order.
    Raises: ValueError when git cannot list the folder (not a repository, git missing, timeout).
    """
    try:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"git ls-files could not run in {root}: {exc}") from exc
    if listed.returncode:
        raise ValueError(f"git ls-files failed in {root}: {listed.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return [rel for rel in listed.stdout.decode("utf-8", "replace").split("\0")
            if rel and os.path.isfile(os.path.join(root, rel))]


def _text(path):
    """Contents of a file as UTF-8 text; None for a binary, oversized, non-UTF-8 or unreadable file."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as stream:
            data = stream.read()
        return None if b"\0" in data else data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def tracked_texts(root, extensions=None):
    """The tracked files of the project with their text, one at a time.

    Parameters: root (str) project folder; extensions (tuple of str or None) lowercase suffixes to keep, all files when None.
    Returns: iterator of (path, text); text is None for a file that is not readable text.
    Raises: ValueError when git cannot list the folder.
    """
    for rel in tracked_files(root):
        if extensions is None or rel.lower().endswith(extensions):
            yield rel, _text(os.path.join(root, rel))


def credentials(root, limit=DEFAULT_LIMIT):
    """Credential shapes in every tracked text file, with the file and line and never the value.

    Parameters: root (str) project folder; limit (int) findings returned.
    Returns: dict {"findings": [{"path", "line", "kind"}], "total": int, "by_kind": {kind: count}, "files_checked": int,
    "files_skipped": int}; skipped files are binary, over `MAX_BYTES` or not UTF-8.
    Raises: ValueError when git cannot list the folder.
    """
    findings, checked, skipped = [], 0, 0
    for rel, text in tracked_texts(root):
        if text is None:
            skipped += 1
            continue
        checked += 1
        findings += [{"path": rel, **item} for item in credential_check.review(None, text)]
    by_kind = {}
    for item in findings:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {"findings": findings[:limit], "total": len(findings), "by_kind": by_kind, "files_checked": checked,
            "files_skipped": skipped}


def links(root, limit=DEFAULT_LIMIT):
    """Relative links of the tracked HTML and Markdown files that point to a missing file, and the links that could not
    be decided (root-relative, template expressions, pages with a base element, unparsable files).

    Parameters: root (str) project folder; limit (int) rows returned in each list.
    Returns: dict {"broken": [{"path", "line", "link"}], "unchecked": [{"path", "line", "link", "why"}],
    "total_broken": int, "total_unchecked": int, "files_checked": int, "files_skipped": int}.
    Raises: ValueError when git cannot list the folder.
    """
    broken, unchecked, checked, skipped = [], [], 0, 0
    for rel, text in tracked_texts(root, link_check.EXTENSIONS):
        if text is None:
            skipped += 1
            continue
        checked += 1
        found = link_check.review(os.path.join(root, rel), None, text)
        broken += [{"path": rel, **item} for item in found["broken"]]
        unchecked += [{"path": rel, **item} for item in found["unchecked"]]
    return {"broken": broken[:limit], "unchecked": unchecked[:limit], "total_broken": len(broken),
            "total_unchecked": len(unchecked), "files_checked": checked, "files_skipped": skipped}
