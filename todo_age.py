"""TODO, FIXME and HACK markers in the comments of the project's code and test files, each with the age of the line that
carries it, oldest first, so a promise nobody kept stands out.

The markers are read from comments only (`comment_check` tokenizers), never from strings or docstrings. The age comes from
one `git blame --porcelain` per file that has a marker: the date of the last commit that changed the line, so a reformatted
line counts as new. Nothing is written.
"""
import datetime
import re
import subprocess
import time

import affected_tests
import code_health
import comment_check
import doc_check

DEFAULT_OLDER_THAN_DAYS = 180
TEXT_LIMIT = 80
_MARKER = re.compile(r"\b(?:TODO|FIXME|HACK)\b")
_HEADER = re.compile(r"^[0-9a-f]{40} \d+ (\d+)")
_UNTRACKED = "no such path"
_NOT_GIT = "not a git repository"


def _comments(path, text):
    """The comments of a source file, [] for a file that does not tokenize."""
    if path.lower().endswith(doc_check.PYTHON):
        return comment_check._python_comments(text)
    return comment_check._braced_comments(text, path.lower().endswith(doc_check.GO))


def markers(path, text):
    """The marker comments of one file.

    Parameters: path (str) project-relative path; text (str) its content.
    Returns: list of {"path", "line", "marker", "text"} in line order, one per comment line that names a marker.
    """
    found = []
    for comment in _comments(path, text):
        for offset, line in enumerate(comment["text"].split("\n")):
            match = _MARKER.search(line)
            if match:
                found.append({"path": path, "line": comment["start"] + offset, "marker": match.group(0),
                              "text": line.strip(" \t\r*/")[:TEXT_LIMIT]})
    return found


def _blame(root, path):
    """The commit time of each line of a file, {line: epoch seconds}, from one `git blame --porcelain`.

    Raises: ValueError when git refuses the file (not a repository, untracked); OSError when git cannot run;
    subprocess.SubprocessError when it times out.
    """
    sha_of, time_of, sha = {}, {}, None
    for row in affected_tests._git(root, "blame", "--porcelain", "--", path).split("\n"):
        header = _HEADER.match(row)
        if header:
            sha = row[:40]
            sha_of[int(header.group(1))] = sha
        elif row.startswith("author-time "):
            time_of[sha] = int(row.split()[1])
    return {line: time_of[sha] for line, sha in sha_of.items() if sha in time_of}


def _reason(error):
    """A line telling why a file could not be aged."""
    message = str(error)
    if _UNTRACKED in message:
        return "untracked or not committed yet: git has no history for it"
    return message or error.__class__.__name__


def scan(root, view_id=None, limit=code_health.DEFAULT_LIMIT, older_than_days=DEFAULT_OLDER_THAN_DAYS):
    """The TODO, FIXME and HACK markers of the indexed project with the age of their lines.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) markers returned;
    older_than_days (int) a marker whose line is older than this many days is flagged `old`.
    Returns: dict {"markers": [{"path", "line", "marker", "text", "age_days", "date", "old"}] oldest first, markers whose
    age could not be read last with `age_days` and `date` None, "counts": {"markers", "old", "undated"},
    "older_than_days": int, "skipped": [{"path", "reason"}] files whose blame failed, "note": str (why no age could be
    read for the whole project, "" otherwise)}. A line changed since the last commit is as old as today.
    Raises: ValueError when the code graph is empty.
    """
    model = code_health._model(root, view_id)
    now = time.time()
    rows, skipped, note, blame = [], [], "", True
    for path in model.files():
        if model.kind_of(path) not in ("code", "test") or not doc_check.supported(path):
            continue
        text = model.read(path)
        found = markers(path, text) if text else []
        if not found:
            continue
        times = {}
        if blame:
            try:
                times = _blame(root, path)
            except ValueError as error:
                if _NOT_GIT in str(error).lower():
                    blame, note = False, "Not a git repository: no marker could be aged."
                else:
                    skipped.append({"path": path, "reason": _reason(error)})
            except OSError as error:
                blame, note = False, f"git could not be run ({error}): no marker could be aged."
            except subprocess.SubprocessError as error:
                skipped.append({"path": path, "reason": f"git blame did not finish: {error}"})
        for row in found:
            stamp = times.get(row["line"])
            age = None if stamp is None else max(0, int((now - stamp) // 86400))
            rows.append({**row, "age_days": age,
                         "date": None if stamp is None else datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%d"),
                         "old": age is not None and age > older_than_days})
    rows.sort(key=lambda r: (r["age_days"] is None, -(r["age_days"] or 0), r["path"], r["line"]))
    return {"markers": rows[:limit],
            "counts": {"markers": len(rows), "old": sum(r["old"] for r in rows),
                       "undated": sum(r["age_days"] is None for r in rows)},
            "older_than_days": older_than_days, "skipped": skipped, "note": note}
