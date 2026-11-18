"""Vulnerable-dependency notice for an edit of a dependency manifest, built before the edit runs.

Manifests: package.json (OSV ecosystem npm), requirements*.txt and pyproject.toml (PyPI: `==` pins and the lower bound
of `>=` and `~=`; pyproject reads `project.dependencies` and `project.optional-dependencies`, not Poetry tables),
go.mod (Go: `require` lines with a plain vX.Y.Z), Cargo.toml (crates.io: registry dependencies in every dependency table,
asked at the lower bound of the requirement) and pom.xml (Maven: `groupId:artifactId` with a plain or property-resolved
version, no ranges and no version inherited from a parent or a BOM). The audit asks about the version a sibling package-lock.json resolved
when it satisfies the range, else about the lower bound. Compares the dependencies of the file before and after the edit,
asks OSV (one `querybatch` call, short timeout) about the added or changed ones only, and answers from a disk cache of
(ecosystem, package, version) -> advisory IDs for 6 hours. Only package names and versions leave the machine. The caller (`hook_decision`) turns a `DependencyCheckError` into
"dependency check skipped: <reason>" and lets the edit go on.
"""
import collections
import json
import os
import re
import time
import tomllib
import xml.etree.ElementTree as ElementTree

import atomic_io
import freshness
import paths

CACHE_PATH = os.path.join(paths.DATA_DIR, "dependency_advisories.json")
CACHE_TTL_S = 6 * 3600
OSV_TIMEOUT_S = 2.5
MAX_PACKAGES = 5
BATCH_SIZE = 100
AUDIT_MAX_MANIFESTS = 50
AUDIT_MAX_DEPTH = 4
AUDIT_SKIP_DIRS = frozenset(("node_modules", "vendor", "dist", "build", "target", "__pycache__", "venv", "site-packages"))
MAX_IDS_SHOWN = 5
TABLES = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
PREFIX = "Smart Tool dependencies: "
_SPECIFIER_RE = re.compile(r"(\^|~|>=|>|=)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?"
                           r"((?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)")
_REQUIREMENTS_FILE_RE = re.compile(r"requirements[A-Za-z0-9._-]*\.txt")
_REQUIREMENT_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
_PIP_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+!_-]*")
_GO_VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")
_LOCKED_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_MAVEN_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+_-]*")
_PROPERTY_RE = re.compile(r"\$\{([^}]+)\}")
_CARGO_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
_TAG_RE = re.compile(r"\*|[xX]|[A-Za-z][A-Za-z0-9._-]*")


class DependencyCheckError(RuntimeError):
    """The check could not run (OSV unreachable or answering badly, unreadable cache, current file not JSON)."""


def _npm_pairs(text):
    """The (name, specifier) pairs of every dependency table in a package.json text.

    Parameters: text (str) file content.
    Returns: dict (name, specifier) -> None, in file order; None when the text is not a JSON object.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pairs = {}
    for table in TABLES:
        entries = data.get(table)
        if isinstance(entries, dict):
            pairs.update({(name, spec): None for name, spec in entries.items() if isinstance(spec, str)})
    return pairs


def _requirements_pairs(text):
    """The (name, specifier) pairs of a requirements.txt text; options, includes, editable and URL lines are skipped.

    Parameters: text (str) file content.
    Returns: dict (name, specifier) -> None in file order; the specifier is the version clauses without spaces ("" when
    the line only names the package).
    """
    pairs = {}
    for raw in text.splitlines():
        line = raw.split(" #")[0].split(";")[0].strip().rstrip("\\").strip()
        if not line or line.startswith(("#", "-")):
            continue
        line = " ".join(word for word in line.split() if not word.startswith("--"))
        match = _REQUIREMENT_RE.fullmatch(line)
        if not match:
            continue
        specifier = "".join(match.group(2).split())
        if specifier and specifier[0] not in "=<>!~":
            continue
        pairs[(match.group(1), specifier)] = None
    return pairs


def _go_pairs(text):
    """The (module, version) pairs of the `require` lines of a go.mod text, single-line and block; replace, exclude and
    the other directives are skipped.

    Parameters: text (str) file content.
    Returns: dict (module, version) -> None in file order.
    """
    pairs, block = {}, None
    for raw in text.splitlines():
        tokens = raw.split("//")[0].split()
        if not tokens:
            continue
        if block is None:
            if tokens[0] == "require" and len(tokens) == 3:
                pairs[(tokens[1], tokens[2])] = None
            elif tokens[-1] == "(":
                block = tokens[0]
        elif tokens == [")"]:
            block = None
        elif block == "require" and len(tokens) == 2:
            pairs[(tokens[0], tokens[1])] = None
    return pairs


def _pyproject_pairs(text):
    """The (name, specifier) pairs of `project.dependencies` and `project.optional-dependencies` in a pyproject.toml text.

    Parameters: text (str) file content.
    Returns: dict (name, specifier) -> None in file order, read as requirement lines; None when the text is not TOML.
    """
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(project, dict):
        return {}
    lines = [item for item in project.get("dependencies") or [] if isinstance(item, str)]
    optional = project.get("optional-dependencies")
    for group in optional.values() if isinstance(optional, dict) else ():
        lines += [item for item in group if isinstance(item, str)]
    return _requirements_pairs("\n".join(lines))


def _cargo_pairs(text):
    """The (crate, requirement) pairs of the registry dependencies of a Cargo.toml text: the dependency, dev-dependency and
    build-dependency tables, the `target.*` tables and `workspace.dependencies`. Path, git and `workspace = true` entries
    have no registry version and are skipped; a `package` key renames the crate.

    Parameters: text (str) file content.
    Returns: dict (crate, requirement) -> None in file order ("" when a registry entry names no version); None when the
    text is not TOML.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    groups = [data]
    targets = data.get("target")
    groups += [group for group in targets.values() if isinstance(group, dict)] if isinstance(targets, dict) else []
    workspace = data.get("workspace")
    groups += [workspace] if isinstance(workspace, dict) else []
    pairs = {}
    for group in groups:
        for table in _CARGO_TABLES:
            entries = group.get(table)
            for name, value in entries.items() if isinstance(entries, dict) else ():
                if isinstance(value, str):
                    pairs[(name, value)] = None
                elif isinstance(value, dict) and not {"path", "git", "workspace"} & value.keys():
                    version = value.get("version")
                    pairs[(value.get("package") if isinstance(value.get("package"), str) else name,
                           version if isinstance(version, str) else "")] = None
    return pairs


def _pom_pairs(text):
    """The (`groupId:artifactId`, version) pairs of every `dependency` element of a pom.xml text, dependency management
    included. `${property}` versions take the value of the pom's `properties` and of `project.groupId|artifactId|version`;
    one that stays unresolved is kept as written, so it reports as not verified.

    Parameters: text (str) file content.
    Returns: dict (name, version) -> None in document order ("" when no version is declared); None when the text is not
    XML or declares entities.
    """
    if "<!ENTITY" in text:
        return None
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    local = lambda element: element.tag.rpartition("}")[2]  # noqa: E731
    child = lambda element, tag: next((item.text or "" for item in element if local(item) == tag), "").strip()  # noqa: E731
    properties = {f"project.{tag}": child(root, tag) for tag in ("groupId", "artifactId", "version")}
    properties.update({local(item): (item.text or "").strip()
                       for block in root if local(block) == "properties" for item in block})
    resolve = lambda value: _PROPERTY_RE.sub(lambda match: properties.get(match.group(1)) or match.group(0), value)  # noqa: E731
    return {(resolve(f"{child(item, 'groupId')}:{child(item, 'artifactId')}"), resolve(child(item, "version"))): None
            for item in root.iter() if local(item) == "dependency"}


def ecosystem_of(path):
    """The ecosystem key of a manifest file name; `_ECOSYSTEMS[key].osv` is the name OSV knows it by.

    Parameters: path (str) file path.
    Returns: "npm" for package.json, "PyPI" for requirements*.txt, "pyproject" for pyproject.toml, "Go" for go.mod, "Cargo" for
    Cargo.toml, "Maven" for pom.xml; None for any other file.
    """
    name = os.path.basename(path)
    if name == "package.json":
        return "npm"
    if name == "go.mod":
        return "Go"
    if name == "pyproject.toml":
        return "pyproject"
    if name == "Cargo.toml":
        return "Cargo"
    if name == "pom.xml":
        return "Maven"
    return "PyPI" if _REQUIREMENTS_FILE_RE.fullmatch(name) else None


def changed_dependencies(before, after, ecosystem="npm"):
    """Dependencies added or whose specifier changed between two manifest texts.

    Parameters: before (str or None) text before the edit, None when the file did not exist; after (str) text after;
    ecosystem (str) one of ECOSYSTEMS.
    Returns: list of (name, specifier); empty when `after` is a package.json that is not valid JSON (mid-edit).
    Raises: DependencyCheckError when `before` exists but is a package.json that is not valid JSON.
    """
    parse = _ECOSYSTEMS[ecosystem].parse
    now = parse(after)
    if now is None:
        return []
    if before is None or not before.strip():
        old = {}
    else:
        old = parse(before)
        if old is None:
            raise DependencyCheckError(f"current {_ECOSYSTEMS[ecosystem].file} is not valid {_ECOSYSTEMS[ecosystem].syntax}")
    return [pair for pair in now if pair not in old]


def _npm_version(specifier):
    """The npm case of `queried_version`: the exact version or the lower bound of a ^, ~, >= or > range; None for tags,
    `*`, URLs, file:, git, workspace:, npm: aliases, x-ranges and compound ranges."""
    match = _SPECIFIER_RE.fullmatch(specifier.strip())
    if not match:
        return None
    operator, major, minor, patch, tail = match.groups()
    parts = [major, minor, patch]
    if operator in ("^", "~", ">=", ">"):
        return ".".join(part or "0" for part in parts) + (tail or "")
    if None in parts:
        return None
    return ".".join(parts) + (tail or "")


def _pip_version(specifier):
    """The PyPI case of `queried_version`: the `==` version, else the first `>=` or `~=` lower bound; None for no
    specifier, `===`, wildcards, `<`, `>`, `!=` alone and anything that is not a version."""
    clauses = specifier.split(",")
    for operators in (("==",), (">=", "~=")):
        for clause in clauses:
            for operator in operators:
                if clause.startswith(operator) and _PIP_VERSION_RE.fullmatch(clause[len(operator):]):
                    return clause[len(operator):]
    return None


def _go_version(specifier):
    """The Go case of `queried_version`: a plain vX.Y.Z; None for pseudo-versions, pre-releases and +incompatible."""
    return specifier if _GO_VERSION_RE.fullmatch(specifier) else None


def _cargo_version(specifier):
    """The Cargo case of `queried_version`: the lower bound of a bare, `^`, `~`, `=`, `>=` or `>` requirement padded to
    three parts; None for `*`, wildcards, `<`, compound requirements and anything else."""
    match = _SPECIFIER_RE.fullmatch(specifier.strip())
    if not match:
        return None
    _operator, major, minor, patch, tail = match.groups()
    return f"{major}.{minor or 0}.{patch or 0}{tail or ''}"


def _maven_version(specifier):
    """The Maven case of `queried_version`: a plain version; None for ranges, unresolved properties and no version."""
    return specifier if _MAVEN_VERSION_RE.fullmatch(specifier) else None


def queried_version(specifier, ecosystem="npm"):
    """The version to ask OSV about for a specifier.

    Parameters: specifier (str) the value in the dependency table; ecosystem (str) one of ECOSYSTEMS.
    Returns: str, the exact version or the lower bound of the range; None when the specifier cannot be reduced safely.
    """
    return _ECOSYSTEMS[ecosystem].version(specifier)


_Ecosystem = collections.namedtuple("_Ecosystem", "parse version label exact osv file syntax")
_PIP_LABEL = lambda name, spec: f"{name}{spec}"  # noqa: E731
_PIP_EXACT = lambda spec, version: spec == "==" + version  # noqa: E731
_ECOSYSTEMS = {
    "npm": _Ecosystem(_npm_pairs, _npm_version, lambda name, spec: f"{name}@{spec}", lambda spec, version: spec == version,
                      "npm", "package.json", "JSON"),
    "PyPI": _Ecosystem(_requirements_pairs, _pip_version, _PIP_LABEL, _PIP_EXACT, "PyPI", "requirements file", "text"),
    "pyproject": _Ecosystem(_pyproject_pairs, _pip_version, _PIP_LABEL, _PIP_EXACT, "PyPI", "pyproject.toml", "TOML"),
    "Go": _Ecosystem(_go_pairs, _go_version, lambda name, spec: f"{name}@{spec}", lambda spec, version: spec == version,
                     "Go", "go.mod", "text"),
    "Cargo": _Ecosystem(_cargo_pairs, _cargo_version, lambda name, spec: f"{name}@{spec}", lambda spec, version: spec in (
        version, "=" + version), "crates.io", "Cargo.toml", "TOML"),
    "Maven": _Ecosystem(_pom_pairs, _maven_version, lambda name, spec: f"{name}@{spec}" if spec else name,
                        lambda spec, version: spec == version, "Maven", "pom.xml", "XML"),
}
ECOSYSTEMS = tuple(_ECOSYSTEMS)


def _load_cache():
    """The cache as {"name@version": {"ids": [...], "at": epoch}}; empty when the file does not exist.

    Raises: DependencyCheckError when the file exists and is unreadable, not JSON or not an object (never reset).
    """
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError) as exc:
        raise DependencyCheckError(f"advisory cache {CACHE_PATH} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise DependencyCheckError(f"advisory cache {CACHE_PATH} is not a JSON object")
    return data


def _fresh_ids(entry, now):
    """The cached IDs of an entry, or None when it is missing, malformed or older than CACHE_TTL_S."""
    if not isinstance(entry, dict) or not isinstance(entry.get("ids"), list) or not isinstance(entry.get("at"), (int, float)):
        return None
    return entry["ids"] if now - entry["at"] < CACHE_TTL_S else None


def _advisories(ecosystem, queries):
    """Advisory IDs for (name, version) pairs, from the cache and one OSV batch for the rest.

    Parameters: ecosystem (str) the OSV ecosystem name (`_Ecosystem.osv`); queries (list of (name, version)).
    Returns: tuple (list of IDs or None per query in order, str cache-write problem or "").
    Raises: DependencyCheckError when the cache is corrupt or OSV cannot answer.
    """
    cache, now = _load_cache(), time.time()
    key = lambda query: f"{ecosystem}:{query[0]}@{query[1]}"  # noqa: E731
    found = {query: _fresh_ids(cache.get(key(query)), now) for query in queries}
    missing = [query for query in queries if found[query] is None]
    if not missing:
        return [found[query] for query in queries], ""
    try:
        answered = []
        for start in range(0, len(missing), BATCH_SIZE):
            answered += freshness.advisories_batch(ecosystem, missing[start:start + BATCH_SIZE], OSV_TIMEOUT_S)
    except RuntimeError as exc:
        raise DependencyCheckError(str(exc)) from exc
    for query, ids in zip(missing, answered):
        found[query] = ids
        if ids is not None:
            cache[key(query)] = {"ids": ids, "at": now}
    cache = {key: entry for key, entry in cache.items() if _fresh_ids(entry, now) is not None}
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        atomic_io.write_secret_text(CACHE_PATH, json.dumps(cache, indent=1))
    except OSError as exc:
        return [found[query] for query in queries], f"advisory cache not saved: {exc}"
    return [found[query] for query in queries], ""


def notice(before, after, ecosystem="npm"):
    """The dependency notice for one manifest edit.

    Parameters: before (str or None) file text before the edit; after (str) text after it; ecosystem (str) one of
    ECOSYSTEMS, from `ecosystem_of`.
    Returns: str, "Smart Tool dependencies: ..." lines for the changed dependencies that have advisories or could not
    be verified, "" when there is nothing to say.
    Raises: DependencyCheckError when the check could not run.
    """
    spec = _ECOSYSTEMS[ecosystem]
    changed = changed_dependencies(before, after, ecosystem)
    handled, extra = changed[:MAX_PACKAGES], len(changed) - MAX_PACKAGES
    lines, queries = {}, []
    for name, specifier in handled:
        version = spec.version(specifier) if freshness.NAME_RES[spec.osv].fullmatch(name) else None
        if version is None:
            lines[(name, specifier)] = f"`{spec.label(name, specifier)}` specifier not verified."
        else:
            queries.append((name, specifier, version))
    problem = ""
    if queries:
        ids, problem = _advisories(spec.osv, [(name, version) for name, _specifier, version in queries])
        for (name, specifier, version), advisories in zip(queries, ids):
            label = f"`{spec.label(name, specifier)}`" + ("" if spec.exact(specifier, version) else f" (checked {version})")
            if advisories is None:
                lines[(name, specifier)] = f"{label} advisory check incomplete, not verified."
            elif advisories:
                shown = ", ".join(advisories[:MAX_IDS_SHOWN])
                more = f" and {len(advisories) - MAX_IDS_SHOWN} more" if len(advisories) > MAX_IDS_SHOWN else ""
                lines[(name, specifier)] = f"{label} has advisory IDs {shown}{more}."
    parts = [lines[pair] for pair in handled if pair in lines]
    if extra > 0:
        parts.append(f"{extra} more not checked.")
    if problem and parts:
        parts.append(f"({problem})")
    return PREFIX + " ".join(parts) if parts else ""


def manifests(root):
    """Project-relative paths of the dependency manifests under root (package.json, requirements*.txt, pyproject.toml,
    go.mod, Cargo.toml, pom.xml), at most
    AUDIT_MAX_MANIFESTS and AUDIT_MAX_DEPTH folders deep, skipping hidden and dependency folders."""
    found = []
    base_depth = os.path.abspath(root).count(os.sep)
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name not in AUDIT_SKIP_DIRS)
        if folder.count(os.sep) - base_depth >= AUDIT_MAX_DEPTH:
            dirs[:] = []
        found += [os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")
                  for name in sorted(files) if ecosystem_of(name)]
    return found[:AUDIT_MAX_MANIFESTS]


def _locked_versions(folder):
    """The versions a package-lock.json next to a package.json resolved, by package name.

    Parameters: folder (str) the folder of the package.json.
    Returns: dict name -> version of the top-level installs (lockfile v2 and v3 `packages`, v1 `dependencies`); empty
    when there is no lockfile or it cannot be read.
    """
    try:
        with open(os.path.join(folder, "package-lock.json"), encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    found = {}
    for name, entry in (data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("version"), str):
            found[name] = entry["version"]
    prefix = "node_modules/"
    for key, entry in (data.get("packages") if isinstance(data.get("packages"), dict) else {}).items():
        if key.startswith(prefix) and "/node_modules/" not in key and isinstance(entry, dict) \
                and isinstance(entry.get("version"), str):
            found[key[len(prefix):]] = entry["version"]
    return found


def _locked_version(specifier, locked):
    """The lockfile version to ask about for an npm specifier, or None when the lock does not apply to it.

    Parameters: specifier (str) the value in the dependency table; locked (str or None) the resolved version.
    Returns: `locked` when it satisfies the specifier (an exact pin, `^` on the same major, `~` on the same minor, `>=` and
    `>` at or above the bound) or the specifier is a dist-tag, `*` or an x-range; None for a stale lock, a lock that is not
    a plain version and specifiers that point elsewhere (URLs, git, file:, workspace:, aliases, compound ranges).
    """
    resolved = _LOCKED_RE.match(locked or "")
    if not resolved:
        return None
    if _TAG_RE.fullmatch(specifier.strip()):
        return locked
    match = _SPECIFIER_RE.fullmatch(specifier.strip())
    if not match:
        return None
    operator, major, minor, patch, _tail = match.groups()
    have = tuple(int(part) for part in resolved.groups())
    bound = tuple(int(part or 0) for part in (major, minor, patch))
    if operator in (">=", ">"):
        ok = have >= bound
    elif operator == "^":
        ok = have >= bound and have[0] == bound[0]
    elif operator == "~":
        ok = have >= bound and have[:2] == bound[:2]
    else:
        ok = None not in (minor, patch) and have == bound
    return locked if ok else None


def audit(root):
    """Advisories of every dependency the manifests of a project declare, not only the ones an edit changed.

    Parameters: root (str) project folder.
    Returns: dict {"checked": packages asked about, "manifests": [{"path", "ecosystem", "packages", "vulnerable":
    [{"package", "specifier", "version", "advisories"}], "unverified": [str]}], "note": str}. A manifest that cannot be
    read or parsed is listed with its reason in `unverified`. For a package.json with a package-lock.json beside it, the
    version asked about is the locked one when it satisfies the range (reported in `version`). One OSV `querybatch` per
    BATCH_SIZE packages and ecosystem; the 6-hour advisory cache is shared with the edit notice.
    Raises: DependencyCheckError when OSV or the cache cannot answer.
    """
    report, plans, queries = [], [], {}
    for rel in manifests(root):
        spec = _ECOSYSTEMS[ecosystem_of(rel)]
        ecosystem = spec.osv
        entry = {"path": rel, "ecosystem": ecosystem, "packages": 0, "vulnerable": [], "unverified": []}
        report.append(entry)
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as stream:
                pairs = spec.parse(stream.read())
        except (OSError, UnicodeDecodeError) as exc:
            entry["unverified"].append(f"manifest not read: {type(exc).__name__}")
            continue
        if pairs is None:
            entry["unverified"].append(f"manifest is not valid {spec.syntax}")
            continue
        entry["packages"] = len(pairs)
        locks = _locked_versions(os.path.dirname(os.path.join(root, rel))) if ecosystem == "npm" else {}
        for name, specifier in pairs:
            version = None
            if freshness.NAME_RES[ecosystem].fullmatch(name):
                version = _locked_version(specifier, locks.get(name)) or spec.version(specifier)
            if version is None:
                entry["unverified"].append(f"{spec.label(name, specifier)}: specifier not verified")
            else:
                plans.append((entry, ecosystem, name, specifier, version, spec.label))
                queries.setdefault(ecosystem, {})[(name, version)] = None
    answers, problem = {}, ""
    for ecosystem, wanted in queries.items():
        keys = list(wanted)
        ids, problem = _advisories(ecosystem, keys)
        answers[ecosystem] = dict(zip(keys, ids))
    for entry, ecosystem, name, specifier, version, describe in plans:
        advisories = answers[ecosystem][(name, version)]
        label = describe(name, specifier)
        if advisories is None:
            entry["unverified"].append(f"{label}: advisory check incomplete")
        elif advisories:
            entry["vulnerable"].append({"package": name, "specifier": specifier, "version": version,
                                        "advisories": advisories})
    total = sum(len(wanted) for wanted in queries.values())
    return {"checked": total, "manifests": report, "note": problem}
