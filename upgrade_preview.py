"""What bumping a dependency changes, asked before the manifest is edited: the version the project's manifests declare, the
advisories OSV knows for that version and for the target, the release notes of the target version and the tests that
import the package. Nothing is edited; every part that cannot be answered says why instead of coming back empty.

The release notes are read only through the reader the caller passes (the product's `web_fetch` path, which the caller
gives only when the network use `web_fetch` needs is allowed), so this module opens no connection of its own.
"""
import collections
import json
import os
import re
import urllib.parse
from collections import defaultdict

import affected_tests
import code_graph
import dependency_notice
import freshness
import index_profile
import index_scope

DEFAULT_LIMIT = 30
NOTES_MAX_URLS = 4
NOTES_PROMPT = ("What changes in version {to} of {name}{since}? List breaking changes, removed or renamed APIs, "
                "deprecations, changed defaults and migration steps, quoting the version numbers. Say plainly when the "
                "page has nothing about {to}.")
NOTE = ("The declared version is read from the manifests (the lockfile when it satisfies the range); the advisories are "
        "OSV's for exactly that version and the target. The tests are those that import the package or reach, through "
        "imports, a file that does; a Python package is looked for under its own name, so one imported under another "
        "name (PyYAML as yaml) is not seen.")
Web = collections.namedtuple("Web", "page answer")
_REQUEST = re.compile(r"v?\d[0-9A-Za-z.+!_-]*")
_GITHUB = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)")
_CHANGE_LABEL = re.compile(r"change|release|history|news", re.IGNORECASE)
_PYTHON = ("PyPI", "pyproject")


def _bare(version):
    """The version without a leading `v` that precedes a digit."""
    return re.sub(r"^v(?=\d)", "", version.strip())


def _checked(package, to):
    """The package name and the target version as the rest of the module takes them.

    Raises: ValueError when `package` is not a name or `to` is neither absent nor a version.
    """
    if not isinstance(package, str) or not package.strip() or len(package) > 214 or re.search(r"\s", package.strip()):
        raise ValueError("Pass package: the dependency name as the manifest writes it (for example requests or @scope/name).")
    if to is not None and (not isinstance(to, str) or not _REQUEST.fullmatch(to.strip())):
        raise ValueError(f"to must be a version such as 2.1.0 or v2.1.0, not {to!r}.")
    return package.strip(), None if to is None else _bare(to)


def _same(left, right, key):
    """Whether two package names are one dependency; PyPI treats `-`, `_` and `.` alike and ignores case."""
    if key in _PYTHON:
        return re.sub(r"[-_.]+", "-", left).lower() == re.sub(r"[-_.]+", "-", right).lower()
    return left == right


def _declared(root, package):
    """Where the manifests of the project declare `package`.

    Returns: tuple (list of {"path", "ecosystem", "name", "specifier", "version", "version_from", "key"}, list of strings
    naming the manifests that could not be read). `version` is the lockfile's when it satisfies the specifier, else
    the exact version or lower bound the specifier names; None when neither can be reduced to a version.
    """
    found, unread = [], []
    for rel in dependency_notice.manifests(root):
        key = dependency_notice.ecosystem_of(rel)
        spec = dependency_notice._ECOSYSTEMS[key]
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as stream:
                pairs = spec.parse(stream.read())
        except (OSError, UnicodeDecodeError) as exc:
            unread.append(f"{rel}: not read ({type(exc).__name__})")
            continue
        if pairs is None:
            unread.append(f"{rel}: not valid {spec.syntax}")
            continue
        locks = dependency_notice._locked_versions(os.path.dirname(os.path.join(root, rel))) if key == "npm" else {}
        for name, specifier in dict.fromkeys(pairs):
            if _same(name, package, key):
                locked = dependency_notice._locked_version(specifier, locks.get(name)) if key == "npm" else None
                version = locked or spec.version(specifier)
                found.append({"path": rel, "ecosystem": spec.osv, "name": name, "specifier": specifier, "version": version,
                              "version_from": "lockfile" if locked else "specifier" if version else None, "key": key})
    return found, unread


def _advisories(entries, to):
    """Fills `advisories` and `target_version` of each declared entry from OSV, through the cache the edit notice shares.

    `advisories` holds `current` and `target` ({"version", "ids"}; ids None when OSV's answer was incomplete),
    `fixed_by_target` and `new_in_target` (ids only one side has, when both are known) and `problems`, the sentences for
    everything that could not be checked, an unavailable OSV included.

    Returns: list of strings, cache problems worth saying.
    """
    notes, by_ecosystem = [], defaultdict(list)
    for entry in entries:
        by_ecosystem[entry["ecosystem"]].append(entry)
        entry["target_version"] = None if to is None else ("v" + to if entry["key"] == "Go" else to)
        entry["advisories"] = {"current": None, "target": None, "fixed_by_target": [], "new_in_target": [], "problems": []}
        if entry["version"] is None:
            entry["advisories"]["problems"].append(f"the current version cannot be read from the specifier "
                                                   f"{entry['specifier']!r}, so its advisories were not looked up")
    for osv, group in by_ecosystem.items():
        valid = [e for e in group if freshness.NAME_RES[osv].fullmatch(e["name"])]
        for entry in (e for e in group if not freshness.NAME_RES[osv].fullmatch(e["name"])):
            entry["advisories"]["problems"].append(f"{entry['name']!r} is not a name OSV accepts for {osv}")
        wanted = {(e["name"], v): None for e in valid for v in (e["version"], e["target_version"]) if v}
        if not wanted:
            continue
        try:
            ids, problem = dependency_notice._advisories(osv, list(wanted))
        except dependency_notice.DependencyCheckError as exc:
            for entry in valid:
                entry["advisories"]["problems"].append(f"advisory lookup unavailable, so no advisory is known (this is not "
                                                       f"a clean result): {exc}")
            continue
        notes += [problem] if problem else []
        answers = dict(zip(wanted, ids))
        for entry in valid:
            result = entry["advisories"]
            for which, version in (("current", entry["version"]), ("target", entry["target_version"])):
                if version is None:
                    continue
                found = answers[(entry["name"], version)]
                result[which] = {"version": version, "ids": found}
                if found is None:
                    result["problems"].append(f"OSV's answer for {entry['name']} {version} was incomplete, so it is "
                                              "neither clean nor vulnerable")
            if result["current"] and result["target"] and None not in (result["current"]["ids"], result["target"]["ids"]):
                now, then = set(result["current"]["ids"]), set(result["target"]["ids"])
                result["fixed_by_target"], result["new_in_target"] = sorted(now - then), sorted(then - now)
    return notes


def _json(text):
    """The object a registry answered; raises ValueError when the text is not a JSON object (a cut page included)."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the registry answer is not JSON (it may have been cut): {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("the registry answer is not a JSON object")
    return data


def _repository(*texts):
    """`(owner, name)` of the first GitHub repository the texts name, or None."""
    for text in texts:
        found = _GITHUB.search(text) if isinstance(text, str) else None
        if found:
            return found.group(1), found.group(2)
    return None


def _github_urls(repository, to):
    """The pages of a GitHub repository that usually hold the notes of version `to`: the release under the two tag
    spellings, then the changelog file."""
    if repository is None:
        return []
    owner, name = repository
    return [f"https://github.com/{owner}/{name}/releases/tag/v{to}", f"https://github.com/{owner}/{name}/releases/tag/{to}",
            f"https://raw.githubusercontent.com/{owner}/{name}/HEAD/CHANGELOG.md"]


def _locations(entry, to, page):
    """The pages that may hold the release notes of `to`, from what the package registry says about the package.

    Parameters: entry (dict) a declared entry; to (str) the target without a `v`; page (callable) url -> text.
    Returns: list of URLs, the registry's own changelog links first; empty when the registry names no repository.
    """
    key, name = entry["key"], entry["name"]
    if key == "npm":
        doc = _json(page(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}/{to}"))
        repository = doc.get("repository")
        return _github_urls(_repository(repository.get("url") if isinstance(repository, dict) else repository,
                                        doc.get("homepage")), to)
    if key in _PYTHON:
        info = _json(page(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{to}/json")).get("info") or {}
        links = info.get("project_urls") or {}
        written = [url for label, url in links.items() if _CHANGE_LABEL.search(label) and isinstance(url, str) and url]
        return written + _github_urls(_repository(*links.values(), info.get("home_page")), to)
    if key == "Go":
        return _github_urls(_repository(name), to)
    if key == "Cargo":
        crate = _json(page(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")).get("crate") or {}
        return _github_urls(_repository(crate.get("repository"), crate.get("homepage")), to)
    return []


def _release_notes(entries, to, web, web_problem):
    """The release notes of the target version, or why they were not read.

    Returns: dict {"read": bool, "reason": str, "sources": [url], "failed": [{"url", "error"}], "text": str}.
    """
    def unread(reason):
        return {"read": False, "reason": reason, "sources": [], "failed": [], "text": ""}

    if to is None:
        return unread("the release notes were not read: pass to=<version> to name the target version.")
    if web is None:
        return unread("the release notes were not read: the network use web_fetch needs is not available "
                      f"({web_problem or 'no reader given'}).")
    entry = entries[0]
    try:
        urls = list(dict.fromkeys(_locations(entry, to, web.page)))[:NOTES_MAX_URLS]
    except Exception as exc:
        return unread(f"the release notes were not read: where they live could not be found ({type(exc).__name__}: {exc}).")
    if not urls:
        return unread(f"the release notes were not read: no repository or changelog link is known for the "
                      f"{entry['ecosystem']} package {entry['name']}.")
    since = f" (and the versions after {_bare(entry['version'])} that the page shows)" if entry["version"] else ""
    try:
        results = web.answer(urls, NOTES_PROMPT.format(to=to, name=entry["name"], since=since))
    except Exception as exc:
        return unread(f"the release notes were not read: {type(exc).__name__}: {exc}")
    read = [r for r in results if r["ok"]]
    failed = [{"url": r["url"], "error": r["error"]} for r in results if not r["ok"]]
    if not read:
        return {**unread("the release notes were not read: none of the pages answered."), "failed": failed}
    return {"read": True, "reason": "", "sources": [r["url"] for r in read], "failed": failed,
            "text": "\n\n".join(f"[{r['url']}]\n{r['text']}" for r in read)}


def _imports(specifier, entry):
    """Whether an import specifier of the code graph names the package of `entry`."""
    key, name = entry["key"], entry["name"]
    if key in _PYTHON:
        top = specifier.lstrip(".").split(".")[0]
        return bool(top) and _same(top.replace("_", "-"), name, key)
    if key == "Maven":
        group = name.partition(":")[0]
        return specifier == group or specifier.startswith(group + ".")
    if key == "Cargo":
        return specifier.split("::")[0].replace("_", "-") == name.replace("_", "-")
    return specifier == name or specifier.startswith(name + "/")


def _tests(root, entries, view_id):
    """The tests that import the package, directly or through a file that does.

    Returns: tuple (list of {"path", "why"}, int files importing it, str note).
    Raises: ValueError or RuntimeError when the code graph cannot be built.
    """
    data = code_graph.build_current(root, view_id, shared=True)
    files = {item["path"] for item in data.get("files") or []}
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    test_files = {name for name in files if index_profile.kind(name, profile) == "test"}
    direct, reverse = {}, defaultdict(set)
    for dep in data.get("dependencies") or []:
        if dep.get("target"):
            reverse[dep["target"]].add(dep["source"])
        elif dep.get("resolution") == "external" and any(_imports(dep["specifier"], entry) for entry in entries):
            direct.setdefault(dep["source"], dep["specifier"])
    for conftest, users in affected_tests._fixture_users(root, test_files).items():
        reverse[conftest] |= users
    depth = affected_tests._depths(sorted(direct), reverse)
    reaching = sorted((name for name in depth if name in test_files and not affected_tests._support(root, name)),
                      key=lambda name: (depth[name], name))
    tests = [{"path": name, "why": f"imports it ({direct[name]})" if depth[name] == 0 else
              f"reaches a file that imports it ({depth[name]} step{'s' if depth[name] > 1 else ''})"} for name in reaching]
    note = code_graph.left_out_sentence(data) + " Tests beyond the cut are not listed." if data.get("truncated") else ""
    return tests, len(direct), note


def preview(root, package, to=None, view_id=None, limit=DEFAULT_LIMIT, web=None, web_problem=""):
    """What bumping `package` to `to` changes, before the manifest is edited.

    Parameters: root (str) project folder; package (str) the dependency as a manifest names it; to (str or None) the
    target version; view_id (str or None) index view; limit (int) tests listed; web (Web or None) the page reader and the
    prompt reader of `web_fetch`, given only when its network use is allowed; web_problem (str) why `web` is None.
    Returns: dict {"package", "to", "declared": [{"path", "ecosystem", "name", "specifier", "version", "version_from",
    "target_version", "advisories": {"current", "target", "fixed_by_target", "new_in_target", "problems"}}],
    "release_notes": {"read", "reason", "sources", "failed", "text"}, "tests": [{"path", "why"}], "tests_problem": str,
    "counts": {"declared", "tests", "importers"}, "notes": [str], "note": str}. A part that could not run says why in its
    own field; the other parts are still answered.
    Raises: ValueError when `package` is not a name, `to` is not a version or no manifest of the project declares the
    package.
    """
    package, to = _checked(package, to)
    entries, unread = _declared(root, package)
    if not entries:
        searched = "; ".join(unread) if unread else "none was found"
        raise ValueError(f"{package!r} is not declared in any manifest of the project (package.json, requirements*.txt, "
                         f"pyproject.toml, go.mod, Cargo.toml, pom.xml); manifests not read: {searched}.")
    notes = _advisories(entries, to) + [f"manifest {text}" for text in unread]
    release = _release_notes(entries, to, web, web_problem)
    try:
        tests, importers, graph_note = _tests(root, entries, view_id)
        problem = ""
    except (ValueError, RuntimeError) as exc:
        tests, importers, graph_note, problem = [], 0, "", f"the tests were not listed: {exc}"
    notes += [graph_note] if graph_note else []
    return {"package": package, "to": to, "declared": [{k: v for k, v in entry.items() if k != "key"} for entry in entries],
            "release_notes": release, "tests": tests[:limit], "tests_problem": problem,
            "counts": {"declared": len(entries), "tests": len(tests), "importers": importers}, "notes": notes, "note": NOTE}
