"""Whether the project can be tagged now, asked without changing anything: the version of `version.py` against the top
heading of `CHANGELOG.md`, the README status line and the page `site/index.html`, no Unreleased content left in the
changelog, a clean working tree on `main` or `develop`, a tag name still free and no documentation entry out of date. A
check that cannot run is reported as not ok with its reason; nothing is tagged, written or published. Five local scans
(credentials, stale doc references, broken relative links, import cycles, public functions removed or re-signed since the
latest tag) are listed apart as advisories and never decide `ready`.
"""
import os
import re
import subprocess

import affected_tests
import api_breaks
import code_health
import doc_refs
import doc_scope
import project_scan
import release_notes

DEFAULT_LIMIT = 30
BRANCHES = ("main", "develop")
SHOWN = 5
_VERSION_LINE = re.compile(r"""^VERSION\s*=\s*["']([^"']+)["']""", re.M)
_TOP_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b")
_UNRELEASED = re.compile(r"^##\s+unreleased\b", re.I)
_HEADING = re.compile(r"^##\s")
_SEMVER = r"\d+\.\d+\.\d+"
_PINNED_INSTALL = re.compile(rf"@({_SEMVER})\s+(?:install|uninstall)\b")
_CHECKED = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


def _text(root, rel):
    """Contents of a project file.

    Raises: ValueError when the file is missing or is not UTF-8 text.
    """
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise ValueError(f"{rel} is missing from the project folder.")
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def _token(version):
    """A pattern for the version as a whole token: not part of a longer number or dotted name."""
    return re.compile(rf"(?<![\w.]){re.escape(version)}(?!\d|\.\d)")


def _declared(root):
    """The `VERSION = "..."` of version.py.

    Raises: ValueError when the file is missing or has no VERSION assignment.
    """
    found = _VERSION_LINE.search(_text(root, "version.py"))
    if not found:
        raise ValueError('version.py has no VERSION = "x.y.z" line.')
    return found.group(1)


def _changelog_top(root, version):
    """The top versioned heading of the changelog against the declared version."""
    for line in _text(root, "CHANGELOG.md").splitlines():
        found = _TOP_HEADING.match(line)
        if found:
            top = found.group(1)
            return top == version, (f"top heading of CHANGELOG.md is {top}" if top == version
                                    else f"top heading of CHANGELOG.md is {top}, version.py says {version}")
    return False, 'CHANGELOG.md has no versioned heading ("## x.y.z").'


def _changelog_unreleased(root):
    """Whether an Unreleased heading above the top version still holds text."""
    left, unreleased = 0, False
    for line in _text(root, "CHANGELOG.md").splitlines():
        if _TOP_HEADING.match(line):
            break
        if _UNRELEASED.match(line):
            unreleased = True
        elif _HEADING.match(line):
            unreleased = False
        elif unreleased and line.strip():
            left += 1
    else:
        return False, 'CHANGELOG.md has no versioned heading ("## x.y.z"), so Unreleased content cannot be placed.'
    return not left, ("no Unreleased content above the top version" if not left
                      else f"{left} non-blank line(s) are still under Unreleased, above the top version")


def _stale_installs(text, version):
    """The pinned install commands of a text that name another version."""
    return sorted({found for found in _PINNED_INSTALL.findall(text) if found != version})


def _readme(root, version):
    """The README status line, when it has one (any mention of the version otherwise), and its pinned install commands."""
    text = _text(root, "README.md")
    token = _token(version)
    status = [(number, line) for number, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith("**Status:**")]
    if status:
        number, line = status[0]
        problems = [] if token.search(line) else [f"the status line (README.md:{number}) does not state {version}"]
    else:
        problems = [] if token.search(text) else [f"README.md never mentions {version}"]
    stale = _stale_installs(text, version)
    if stale:
        problems.append(f"install commands pin {', '.join(stale)}")
    return not problems, "; ".join(problems) or f"README.md states {version}"


def _site(root, version):
    """The page card and the pinned install command of the product page."""
    text = _text(root, "site/index.html")
    problems = [] if f"{version} beta" in text else [f"the page does not show '{version} beta'"]
    if version not in _PINNED_INSTALL.findall(text):
        problems.append(f"the page has no install command pinned to {version}")
    stale = _stale_installs(text, version)
    if stale:
        problems.append(f"install commands pin {', '.join(stale)}")
    return not problems, "; ".join(problems) or f"site/index.html shows {version} beta and its install command"


def _clean(root):
    """Whether git reports no change, staged, unstaged or untracked."""
    entries = [line for line in affected_tests._git(root, "status", "--porcelain").splitlines() if line.strip()]
    if not entries:
        return True, "working tree is clean"
    shown = ", ".join(line[3:] for line in entries[:SHOWN])
    return False, f"{len(entries)} path(s) with uncommitted changes: {shown}{', ...' if len(entries) > SHOWN else ''}"


def _branch(root):
    """Whether the current branch is one a release is tagged from."""
    name = affected_tests._git(root, "branch", "--show-current").strip()
    if not name:
        return False, "HEAD is detached; check out main or develop"
    return name in BRANCHES, (f"on {name}" if name in BRANCHES else f"on {name}; tag from {' or '.join(BRANCHES)}")


def _tag_free(root, version):
    """Whether the tag name `v<version>` is still unused."""
    tag = f"v{version}"
    if not affected_tests._git(root, "tag", "--list", tag).strip():
        return True, f"{tag} does not exist yet"
    commit = affected_tests._git(root, "rev-parse", "--short", f"{tag}^{{commit}}").strip()
    return False, f"{tag} already exists at {commit}; raise the version before tagging"


def _docs(root, limit):
    """Whether the generated documents match the code; a project without them has nothing to check."""
    if not os.path.isdir(os.path.join(root, doc_scope.DOCS_DIR)):
        return True, f"{doc_scope.DOCS_DIR} is not generated in this project: nothing to check"
    found = doc_scope.check(root, None, limit)
    if found["ok"]:
        return True, "every documentation anchor matches the code"
    shown = "; ".join(f"{item['id']} ({item['issue'].replace('_', ' ')})" for item in found["findings"][:SHOWN])
    return False, f"{found['findings_total']} documentation finding(s): {shown}"


def _credentials(root):
    """Whether no tracked text file holds a credential shape."""
    found = project_scan.credentials(root, SHOWN)
    if not found["total"]:
        return True, f"no credential shape in {found['files_checked']} tracked file(s)"
    where = ", ".join(f"{item['path']}:{item['line']}" for item in found["findings"])
    return False, f"{found['total']} credential shape(s) in tracked files: {where}{', ...' if found['total'] > SHOWN else ''}"


def _doc_refs(root):
    """Whether the README and docs name no path, link or function that is gone."""
    found = doc_refs.check(root, SHOWN)
    return not found["counts"]["stale"], found["summary"]


def _links(root):
    """Whether the relative links of the tracked HTML and Markdown files all point to a file."""
    found = project_scan.links(root, SHOWN)
    if not found["total_broken"]:
        return True, (f"no broken relative link in {found['files_checked']} file(s); "
                      f"{found['total_unchecked']} link(s) could not be decided")
    where = ", ".join(f"{item['path']}:{item['line']}" for item in found["broken"])
    return False, f"{found['total_broken']} broken relative link(s): {where}{', ...' if found['total_broken'] > SHOWN else ''}"


def _cycles(root):
    """Whether the indexed project has no import cycle."""
    found = code_health.import_cycles(root, None, SHOWN)
    if not found["total"]:
        return True, "no import cycle in the indexed code graph"
    return False, f"{found['total']} import cycle(s) covering {found['files_in_cycles']} files"


def _api_breaks(root):
    """Whether no public function was removed or re-signed since the latest tag; a repository without a tag has no release
    to break."""
    found = api_breaks.report(root, None, SHOWN)
    if found["tag"] is None:
        return True, "no tag yet: there is no release to compare the public API with"
    total = found["counts"]["removed"] + found["counts"]["resigned"]
    if not total:
        return True, f"no public function removed or re-signed since {found['tag']}"
    rows = [(row, "removed") for row in found["removed"]] + [(row, "new signature") for row in found["resigned"]]
    shown = ", ".join(f"{row['name']} ({kind}, {row['total']} caller(s))" for row, kind in rows[:SHOWN])
    return False, (f"{total} public function(s) removed or re-signed since {found['tag']}: {shown}"
                   f"{', ...' if total > SHOWN else ''}")


def _run(name, function, *args):
    """One check as {"name", "ok", "detail"}; a failure to run it is a not-ok result carrying the cause."""
    try:
        ok, detail = function(*args)
    except _CHECKED as exc:
        ok, detail = False, f"could not be checked: {exc}"
    return {"name": name, "ok": bool(ok), "detail": detail}


def _commits(root, limit):
    """The commit subjects since the latest tag, the cheap part of the release notes draft, or why there are none."""
    try:
        base = release_notes._base(root, None)
        commits, total = release_notes._commits(root, base, limit)
    except _CHECKED as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "base": base, "commit_total": total, "commits": commits}


def check(root, limit=DEFAULT_LIMIT):
    """The answer to "can I tag?".

    Parameters: root (str) project folder; limit (int) commit subjects and documentation findings listed.
    Returns: dict {"ready": bool (every check ok), "version": str or None, "checks": [{"name", "ok", "detail"}],
    "advisories": [{"name", "ok", "detail"}] (committed credentials, stale doc references, broken relative links, import
    cycles and the public functions removed or re-signed since the latest tag: local scans that never change `ready`), "commits_since_last_tag": {"ok": True, "base", "commit_total", "commits": [{"hash", "subject"}]} or {"ok": False,
    "error"}, "note": str}. Nothing raises for a missing file or a failed git call: that check is not ok with the reason.
    """
    try:
        version = _declared(root)
        checks = [{"name": "version_file", "ok": True, "detail": f"version.py VERSION is {version}"}]
    except _CHECKED as exc:
        version, problem = None, f"could not be checked: {exc}"
        checks = [{"name": "version_file", "ok": False, "detail": problem}]
    for name, function in (("changelog_top_version", _changelog_top), ("readme_version", _readme),
                           ("site_version", _site), ("tag_free", _tag_free)):
        checks.append(_run(name, function, root, version) if version
                      else {"name": name, "ok": False, "detail": f"no version to compare: {problem}"})
    checks.insert(2, _run("changelog_unreleased_empty", _changelog_unreleased, root))
    checks += [_run("working_tree_clean", _clean, root), _run("branch", _branch, root),
               _run("docs_current", _docs, root, limit)]
    advisories = [_run("credentials", _credentials, root), _run("doc_refs", _doc_refs, root),
                  _run("relative_links", _links, root), _run("import_cycles", _cycles, root),
                  _run("api_breaks", _api_breaks, root)]
    return {"ready": all(item["ok"] for item in checks), "version": version, "checks": checks, "advisories": advisories,
            "commits_since_last_tag": _commits(root, limit),
            "note": "The full notes draft, with the touched functions and decision notes, is project_manage action="
                    "release_notes; it analyzes the diff, which takes seconds, so the check lists only the commits."}
