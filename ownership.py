"""Who owns the riskiest files: for each hotspot file, its authors by commit count and by surviving lines, and a flag on the
files where one author holds most of the lines.

The files are those of `code_health.hotspots`; the commits come from `git shortlog` and the lines from `git blame` at HEAD.
Names are printed as git reports them (no addresses). Nothing is written.
"""
from collections import Counter
import subprocess

import affected_tests
import code_health

DEFAULT_LIMIT = 10
DEFAULT_THRESHOLD = 0.8
AUTHORS_SHOWN = 5

RULE = ("per hotspot file: by_commits is `git shortlog -sn --no-merges HEAD` of the file, by_lines is `git blame "
        "--line-porcelain -w HEAD` (the lines that still stand, whitespace-only changes ignored), each author with its "
        "count and its whole-percent share, the first 5 authors of each; top_author and top_share are the author with the "
        "most lines. A file is flagged when top_share reaches the threshold (a fraction of the lines, default 0.8, from "
        "0.5 to 1). Authors are the names git reports; the same person under two names counts twice unless a .mailmap "
        "joins them, and an author who only reformatted or moved code holds those lines.")


def _threshold(value):
    """The flag threshold as a float from 0.5 to 1, the default for None."""
    if value is None:
        return DEFAULT_THRESHOLD
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'threshold' must be a number from 0.5 to 1, got: {value!r}") from None
    if not 0.5 <= number <= 1:
        raise ValueError(f"'threshold' must be a number from 0.5 to 1, got: {value!r}")
    return number


def _shortlog(root, path):
    """The (author, commits) of a file, most commits first."""
    out = affected_tests._git(root, "-c", "core.quotepath=false", "shortlog", "-sn", "--no-merges", "HEAD", "--", path)
    rows = []
    for line in out.splitlines():
        count, _tab, name = line.strip().partition("\t")
        if name and count.isdigit():
            rows.append((name, int(count)))
    return sorted(rows, key=lambda row: (-row[1], row[0]))


def _blame(root, path):
    """The (author, surviving lines) of a file at HEAD, most lines first."""
    out = affected_tests._git(root, "-c", "core.quotepath=false", "blame", "--line-porcelain", "-w", "HEAD", "--", path)
    lines = Counter(line[len("author "):] for line in out.splitlines() if line.startswith("author "))
    return sorted(lines.items(), key=lambda row: (-row[1], row[0]))


def _shares(rows, key):
    total = sum(count for _name, count in rows)
    return [{"author": name, key: count, "share": round(100 * count / total)} for name, count in rows[:AUTHORS_SHOWN]]


def _file_row(root, file, threshold):
    row = {"path": file["path"], "commits": file["commits"], "lines": file["lines"]}
    try:
        by_commits, by_lines = _shortlog(root, file["path"]), _blame(root, file["path"])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return {**row, "authors": 0, "by_commits": [], "by_lines": [], "top_author": None, "top_share": 0,
                "flagged": False, "error": str(error)}
    held = sum(count for _name, count in by_lines)
    top_name, top_count = by_lines[0] if by_lines else (None, 0)
    return {**row, "authors": len({name for name, _count in by_commits} | {name for name, _count in by_lines}),
            "by_commits": _shares(by_commits, "commits"), "by_lines": _shares(by_lines, "lines"),
            "top_author": top_name, "top_share": round(100 * top_count / held) if held else 0,
            "flagged": bool(held) and top_count / held >= threshold}


def report(root, view_id=None, limit=DEFAULT_LIMIT, threshold=None):
    """The authors of the hotspot files, with the files one author holds flagged.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) hotspot files read (each costs a
    `git blame`); threshold (float or None) share of the lines that flags a file, 0.5 to 1, 0.8 when None.
    Returns: dict {"files": [{"path", "commits", "lines", "authors", "by_commits": [{"author", "commits", "share"}],
    "by_lines": [{"author", "lines", "share"}], "top_author", "top_share", "flagged", "error" (only when git failed for
    that file; the rest of its row is empty)}] in hotspot order, "flagged": int, "total": int (hotspot files of the
    project), "threshold": float, "note": str (why no history could be read, "" otherwise), "rule": str}.
    Raises: ValueError when the code graph is empty or the threshold is out of range.
    """
    threshold = _threshold(threshold)
    history = code_health.hotspots(root, view_id, limit)
    files = [_file_row(root, file, threshold) for file in history["files"]]
    return {"files": files, "flagged": sum(row["flagged"] for row in files), "total": history["total"],
            "threshold": threshold, "note": history["note"], "rule": RULE}
