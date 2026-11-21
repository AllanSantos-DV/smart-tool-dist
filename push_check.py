"""Review of the commits a push sends, for the opt-in git pre-push hook (`hooks/pre_push.py`).

Per pushed range it joins what Smart Tool already computes for a change set: the tests to run (`affected_tests`) and the
impact summary (`pr_impact.report`, with the commit the remote holds as its base). The hook that asks for it never blocks
a push; a range that cannot be analyzed is said with its reason, never dropped silently.
"""
import re

import affected_tests
import pr_impact
import removed_callers

MAX_RANGES = 3
REPORT_LIMIT = 15
MAX_LISTED = 8
DECISION_FUNCTIONS = 3
DECISION_NOTES = 2
_SHA = re.compile(r"[0-9a-f]{7,64}")


def _unborn(sha):
    """True for git's all-zero object name: a branch the remote does not have yet, or a deletion."""
    return not sha.strip("0")


def _commit(root, rev):
    """The full name of the commit `rev` names in the repository at `root`, or None when it is not there."""
    try:
        return affected_tests._git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
    except ValueError:
        return None


def ranges_from(raw):
    """The validated pushed ranges of the hook's request.

    Parameters: raw (list) of dicts with `local_ref`, `local`, `remote_ref` and `remote` (git's pre-push line).
    Returns: the same list. Raises ValueError when it is empty or an entry is not what git writes.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("the hook sent no pushed range")
    for item in raw:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in
                                             ("local_ref", "local", "remote_ref", "remote")):
            raise ValueError("a pushed range must hold local_ref, local, remote_ref and remote as text")
        if not (_SHA.fullmatch(item["local"]) and _SHA.fullmatch(item["remote"])):
            raise ValueError(f"the commits of the range {item['local_ref']} are not git object names")
    return raw


def _base(root, remote, pushed):
    """The commit to compare the pushed one with.

    Returns: tuple (str base, str reason): `base` is "" when the range has nothing to analyze, and then `reason`
    says why when the user should hear it ("" when there is nothing new to push).
    """
    if not _unborn(pushed["remote"]):
        base = _commit(root, pushed["remote"])
        if base:
            return base, ""
        return "", f"the commit the remote holds ({pushed['remote'][:10]}) is not in this repository; fetch first"
    new = affected_tests._git(root, "rev-list", "--reverse", pushed["local"], "--not",
                              f"--remotes={remote}" if remote else "--remotes").split()
    if not new:
        return "", ""
    base = _commit(root, f"{new[0]}^")
    if base:
        return base, ""
    return "", (f"the pushed branch is new and no remote-tracking branch of {remote or 'any remote'} gives a commit to "
                "compare it with")


def _listed(label, lines, total=None):
    """`label` and the first MAX_LISTED of `lines`, with how many more there are."""
    shown = lines[:MAX_LISTED]
    total = len(lines) if total is None else total
    more = [f"  ... and {total - len(shown)} more"] if total > len(shown) else []
    return [label] + [f"  {line}" for line in shown] + more


def _summary(report):
    """The lines of the report worth a push: the tests to run first, then only the sections that found something. The
    decision notes keep to the rule of the edit hook (the first DECISION_FUNCTIONS functions, DECISION_NOTES notes each),
    so a push that names a weak match says nothing."""
    counts = report["counts"]
    lines = [f"{counts['changed_files']} changed file(s), {counts['tests']} test file(s) to run."]
    if report["tests"]:
        lines += _listed("Tests affected by the range:", [f"{t['path']}: {t['why']}" for t in report["tests"]],
                         counts["tests"])
        lines += [f"  {c['command']} (in {c['cwd']})" if c.get("command") else f"  {c['reason']} ({', '.join(c['files'])})"
                  for c in report["commands"]]
    if report["run_all"]:
        lines.append("  A changed file can change every test: run the whole suite.")
    if report["untested"]:
        lines += _listed("Touched functions no test reaches:", report["untested"])
    for found, title in ((report["removed_callers"], "Callers still naming a function the range removed or renamed:"),
                         (report["signature_callers"], "Callers of a function whose signature the range changed:")):
        if found:
            lines += _listed(title, [f"{item['name']} ({item['path']}): {removed_callers.sites_text(item)}" for item in found])
    if report["stale_anchors"]:
        lines += _listed("Documentation left out of date:", [f"{f['id']}: {f['issue'].replace('_', ' ')}"
                                                           for f in report["stale_anchors"]])
    if report["edit_checks"]:
        lines += ["Edit checks over the whole range:", f"  {report['edit_checks']}"]
    notes = [f"{label}: {hit['id']} {hit['title']}" for label, hits in list(report["decisions"].items())[:DECISION_FUNCTIONS]
             for hit in hits[:DECISION_NOTES]]
    if notes:
        lines += _listed("Decision notes that name a touched function:", notes)
    if report["decisions_note"]:
        lines.append(report["decisions_note"])
    return lines


def _range_lines(root, remote, pushed):
    """The lines for one pushed range; empty for a deletion or a range with nothing new, a sentence with the reason for
    a range that cannot be analyzed."""
    if _unborn(pushed["local"]):
        return []
    title = f"Smart Tool push review of {pushed['local_ref']}"
    try:
        if _commit(root, pushed["local"]) != _commit(root, "HEAD"):
            return [f"{title}: not analyzed, it is not the checked out commit and the impact is computed on the working tree."]
        base, reason = _base(root, remote, pushed)
        if not base:
            return [f"{title}: not analyzed, {reason}."] if reason else []
        lines = [f"{title} against {base[:10]}:"]
        if affected_tests._git(root, "status", "--porcelain").strip():
            lines.append("The working tree has uncommitted changes; the review includes them.")
        return lines + _summary(pr_impact.report(root, base, REPORT_LIMIT)) + \
            [f"Full report: project_manage action=pr_impact base={base[:10]}"]
    except Exception as exc:
        return [f"{title}: the review failed: {type(exc).__name__}: {exc}"]


def review(root, remote, ranges):
    """Reviews what a push of `ranges` sends from the repository at `root` to `remote`.

    Parameters: root (str) project folder, inside a git repository; remote (str) the remote's name or URL as git passes
    it to the hook ("" when unknown); ranges (list) as `ranges_from` takes them.
    Returns: dict with `text` (what the hook prints; empty when there is nothing to say) and `ranges` (how many were
    looked at; those past MAX_RANGES are named, not analyzed).
    Raises: ValueError when `ranges` is malformed.
    """
    ranges = ranges_from(ranges)
    lines = []
    for pushed in ranges[:MAX_RANGES]:
        lines += _range_lines(root, remote if isinstance(remote, str) else "", pushed)
    if len(ranges) > MAX_RANGES:
        lines.append(f"{len(ranges) - MAX_RANGES} more pushed range(s) were not reviewed: the review reads the first "
                     f"{MAX_RANGES}.")
    return {"text": "\n".join(lines), "ranges": min(len(ranges), MAX_RANGES)}
