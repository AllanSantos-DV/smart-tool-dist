"""A Markdown draft for the next release, built without a model from facts the project already holds: the commit
subjects since a base, the public functions the diff touches with their caller and test counts, the decision notes that
name them and the documentation entries the diff leaves out of date. The sections come from the builders of
`pr_impact`; every line is a commit subject, a name or a count read from the project, never written text. Nothing is
published or written.
"""
import affected_tests
import doc_check
import pr_impact

DEFAULT_LIMIT = 30
CALLBACK_SUFFIX = " (callback"


def _base(root, base):
    """The revision the draft starts from.

    Parameters: root (str) project folder; base (str or None) a tag, branch or commit, the latest tag of the history of
    HEAD when None.
    Returns: str the revision.
    Raises: ValueError when the folder is not a git repository, no tag exists for the default, or the base is not a commit.
    """
    affected_tests._git(root, "rev-parse", "--git-dir")
    if base is None:
        try:
            return affected_tests._git(root, "describe", "--tags", "--abbrev=0").strip()
        except ValueError as exc:
            raise ValueError(f"The repository has no tag to start from ({exc}). Pass base: a tag, branch or commit.") from None
    if not isinstance(base, str) or not base.strip() or base.startswith("-"):
        raise ValueError("base must be a git tag, branch or commit.")
    try:
        affected_tests._git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except ValueError:
        raise ValueError(f"base {base!r} is not a commit of this repository.") from None
    return base


def _commits(root, base, limit):
    """Hash and subject of the commits reachable from HEAD and not from the base, newest first.

    Returns: tuple (list of {"hash", "subject"} up to `limit`, int total).
    """
    rows = [line.partition("\0")[::2] for line in
            affected_tests._git(root, "log", "--format=%h%x00%s", f"{base}..HEAD").splitlines() if line]
    return [{"hash": short, "subject": subject} for short, subject in rows[:limit]], len(rows)


def _public(label):
    """Whether a `path::name` label names a public function: no segment of the name is private (a leading `_` that is not a
    dunder, or `#`)."""
    qualified = label.rpartition("::")[2].split("(")[0]
    return all(doc_check._public_name(part) and not part.startswith("#") for part in qualified.split("."))


def _commit_lines(commits, total, base):
    """The Markdown section of the commit subjects."""
    if not total:
        return ["### Commits", "", f"No commit since {base}.", ""]
    lines = [f"### Commits ({total})", ""] + [f"- {item['hash']} {item['subject']}" for item in commits]
    if total > len(commits):
        lines.append(f"- ... and {total - len(commits)} more commit(s)")
    return lines + [""]


def _function_lines(base, shown, hidden, callers):
    """The Markdown section of the touched public functions with their caller and test counts."""
    lines = ["### Public functions touched and who calls them", "", f"Diff of {base} against the working tree.", ""]
    if not shown:
        return lines + ["No public function is touched by the diff.", ""]
    lines += ["| Function | Callers | Tests |", "|---|---|---|"]
    for label in shown:
        reached = callers[label]
        lines.append(f"| `{label}` | not in the graph | - |" if reached is None
                     else f"| `{label}` | {reached[1]} | {reached[2] or 'none'} |")
    return lines + (["", f"{hidden} more public function(s) not listed."] if hidden else []) + [""]


def _decision_lines(shown, decisions, note):
    """The Markdown section of the decision notes that name a touched function, from `pr_impact._decisions`."""
    lines = ["### Decision notes of the touched functions", ""]
    lines += [f"- `{label}`: `{hit['id']}` {hit['title']}" for label, hits in decisions.items() for hit in hits]
    if not decisions:
        lines.append("No decision note or commit message names a touched public function." if shown
                     else "No public function is touched, so no decision note was searched.")
    return lines + ([note] if note else []) + [""]


def _stale_lines(stale, note, limit):
    """The Markdown section of the documentation entries the diff leaves out of date, from `pr_impact._stale`."""
    lines = ["### Documentation left out of date", ""]
    if note:
        return lines + [note, ""]
    if not stale:
        return lines + ["No documented entry in the changed files is out of date.", ""]
    lines += [f"- `{f['id']}`: {f['issue'].replace('_', ' ')} ({f['file']}:{f['line']})" for f in stale[:limit]]
    if len(stale) > limit:
        lines.append(f"- ... and {len(stale) - limit} more")
    return lines + [""]


def draft(root, base=None, limit=DEFAULT_LIMIT, view_id=None):
    """The release notes draft since a base.

    Parameters: root (str) project folder; base (str or None) tag, branch or commit the draft starts from, the latest tag
    when None; limit (int) rows in each list; view_id (str or None) index view.
    Returns: dict {"base", "markdown" (headed `## Draft since <base>`), "commits": [{"hash", "subject"}], "commit_total",
    "functions": [path::name shown], "callers": {path::name: caller count}, "decisions", "stale_anchors", "stale_note"}.
    Raises: ValueError when the folder is not a git repository, there is no tag and no base was given, or the base is not a
    commit; RuntimeError when the code graph cannot be built.
    """
    base = _base(root, base)
    changes = affected_tests.affected(root, base, pr_impact.ALL_ROWS, view_id)
    touched = list(dict.fromkeys(label.partition(CALLBACK_SUFFIX)[0] for label in changes["touched_functions"]))
    public = [label for label in touched if _public(label)]
    shown = public[:limit]
    callers = {label: pr_impact._callers(root, label, view_id) for label in shown}
    decisions, decisions_note = pr_impact._decisions(root, shown)
    stale, stale_note = pr_impact._stale(root, view_id, set(changes["changed"]["code"]))
    commits, total = _commits(root, base, limit)
    lines = [f"## Draft since {base}", ""] + _commit_lines(commits, total, base)
    lines += _function_lines(base, shown, len(public) - len(shown), callers)
    lines += _decision_lines(shown, decisions, decisions_note) + _stale_lines(stale, stale_note, limit)
    return {"base": base, "markdown": "\n".join(lines).rstrip("\n") + "\n", "commits": commits, "commit_total": total,
            "functions": shown, "callers": {label: reached[1] if reached else 0 for label, reached in callers.items()},
            "decisions": decisions, "stale_anchors": stale[:limit], "stale_note": stale_note}
