"""Review of the commits a push sends, for the opt-in git pre-push hook (`hooks/pre_push.py`).

Per pushed range it joins what Smart Tool already computes for a change set: the tests to run (`affected_tests`) and the
impact summary (`pr_impact.report`, with the commit the remote holds as its base). The hook that asks for it never blocks
a push; a range that cannot be analyzed is said with its reason, never dropped silently.
"""
import os
import re

import affected_tests
import doc_check
import doc_scope
import dependencies_section
import doc_refs_section
import doc_validate
import duplicates_section
import index_profile
import index_scope
import new_links_section
import pr_impact
import removed_callers

MAX_RANGES = 3
MAX_DOC_FILES = 50
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


def _advisory(section, title, describe, label):
    """The lines of one advisory section of the report: its findings under `title`, or one line with the reason when the
    check could not run; none when it found nothing."""
    if section["findings"]:
        return _listed(title, [describe(item) for item in section["findings"]])
    return [] if section["checked"] else [f"{label} not checked: {section['problem']}."]


def _summary(report):
    """The lines of the report worth a push: the tests to run first, then only the sections that found something (the
    duplicates, documentation references and dependencies sections also say when they could not be checked). The
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
    copies = report["duplicates"]
    if copies["findings"]:
        lines += _listed("Functions of the range that duplicate existing code (advisory):",
                         [duplicates_section.describe(item) for item in copies["findings"]])
    elif not copies["checked"]:
        lines.append(f"Duplicates not checked: {copies['problem']}.")
    lines += _advisory(report["doc_refs"], "Documentation that still names code the range removed (advisory):",
                       doc_refs_section.describe, "Documentation references")
    lines += _advisory(report["dependencies"], "Dependencies the range leaves out of step with the manifests (advisory):",
                       dependencies_section.describe, "Dependencies")
    links = report["new_links"]
    if links["findings"]:
        lines += _listed("New external links of the range that may be dead (advisory):",
                         [new_links_section.describe(item) for item in links["findings"]])
    if links["problem"]:
        lines.append(f"New links not checked: {links['problem']}.")
    if links["not_checked"]:
        lines.append(new_links_section.cut_sentence(links["not_checked"]))
    notes = [f"{label}: {hit['id']} {hit['title']}" for label, hits in list(report["decisions"].items())[:DECISION_FUNCTIONS]
             for hit in hits[:DECISION_NOTES]]
    if notes:
        lines += _listed("Decision notes that name a touched function:", notes)
    if report["decisions_note"]:
        lines.append(report["decisions_note"])
    return lines


def _public_touched(root, base):
    """The public functions of the code files the range changes that the range adds or edits, as the documentation
    rule sees them (`doc_check.functions`: public by the language convention, not nested, not an override).

    Returns: tuple (list of dicts `path`, `name`, `documented`, `anchor`; int code files past MAX_DOC_FILES that were
    not read).
    """
    changes = affected_tests._changes(root, base)
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    paths = [path for path, change in sorted(changes.items())
             if change["status"] != "deleted" and doc_check.supported(path) and index_profile.kind(path, profile) == "code"]
    found = []
    for path in paths[:MAX_DOC_FILES]:
        try:
            with open(os.path.join(root, *path.split("/")), encoding="utf-8") as stream:
                source = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        functions = [f for f in doc_check.functions(path, source) if f.get("kind", "function") in ("function", "method")]
        anchors, lines = doc_scope.anchor_ids(functions, path), changes[path]["lines"]
        found += [{"path": path, "name": f["name"], "documented": f["documented"], "anchor": anchors[f["start"]]}
                  for f in functions if f["required"]
                  and (lines is None or lines.intersection(range(f["start"], f["end"] + 1)))]
    return found, max(len(paths) - MAX_DOC_FILES, 0)


def _docs_lines(root, base):
    """The documentation section of a pushed range (the `docs=true` option of the hook): the public functions the range
    adds or edits that have no docstring or whose technical entry under docs/scope has no Feature line or does not exist.
    A section with nothing to say is left out; a check that could not run is said."""
    try:
        functions, left = _public_touched(root, base)
        if not functions:
            return []
        generated = os.path.isdir(os.path.join(root, doc_scope.DOCS_DIR))
        sections = doc_validate.technical_sections(root) if generated else {}
        problems = []
        for item in functions:
            label = f"{item['path']}::{item['name']}"
            if not item["documented"]:
                problems.append(f"{label}: no docstring")
            if not generated:
                continue
            entry = sections.get(item["anchor"])
            if entry is None:
                problems.append(f"{label}: no entry in {doc_scope.DOCS_DIR}, regenerate it")
            elif not doc_validate.has_feature_line(entry):
                problems.append(f"{label}: its technical entry has no Feature line")
        lines = _listed("Public functions of the range without documentation:", problems) if problems else []
        if not generated:
            lines.append(f"Feature lines not checked: {doc_scope.DOCS_DIR} does not exist here "
                         "(project_manage action=docs generate=true).")
        if left:
            lines.append(f"{left} changed code file(s) were not read for documentation: the check reads the first "
                         f"{MAX_DOC_FILES}.")
        return lines
    except Exception as exc:
        return [f"The documentation check failed: {type(exc).__name__}: {exc}"]


def _range_lines(root, remote, pushed, docs=False, links=False):
    """The lines for one pushed range; empty for a deletion or a range with nothing new, a sentence with the reason for
    a range that cannot be analyzed. With `docs` the documentation section follows the summary; with `links` the summary
    includes the check of the links the range adds."""
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
        return lines + _summary(pr_impact.report(root, base, REPORT_LIMIT, links=links)) + (_docs_lines(root, base) if docs else []) + \
            [f"Full report: project_manage action=pr_impact base={base[:10]}"]
    except Exception as exc:
        return [f"{title}: the review failed: {type(exc).__name__}: {exc}"]


def review(root, remote, ranges, docs=False, links=False):
    """Reviews what a push of `ranges` sends from the repository at `root` to `remote`.

    Parameters: root (str) project folder, inside a git repository; remote (str) the remote's name or URL as git passes
    it to the hook ("" when unknown); ranges (list) as `ranges_from` takes them; docs (bool) add the documentation
    section of each range; links (bool) read the external links each range adds and say which look dead (network use).
    Returns: dict with `text` (what the hook prints; empty when there is nothing to say) and `ranges` (how many were
    looked at; those past MAX_RANGES are named, not analyzed).
    Raises: ValueError when `ranges` is malformed or `docs` or `links` is not a boolean.
    """
    if type(docs) is not bool:
        raise ValueError("docs must be true or false")
    if type(links) is not bool:
        raise ValueError("links must be true or false")
    ranges = ranges_from(ranges)
    lines = []
    for pushed in ranges[:MAX_RANGES]:
        lines += _range_lines(root, remote if isinstance(remote, str) else "", pushed, docs, links)
    if len(ranges) > MAX_RANGES:
        lines.append(f"{len(ranges) - MAX_RANGES} more pushed range(s) were not reviewed: the review reads the first "
                     f"{MAX_RANGES}.")
    return {"text": "\n".join(lines), "ranges": min(len(ranges), MAX_RANGES)}
