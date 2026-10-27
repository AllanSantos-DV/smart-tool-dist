"""Vulnerable-dependency notice for an edit of a dependency manifest, built before the edit runs.

Manifests: package.json (OSV ecosystem npm), requirements*.txt (PyPI: `==` pins and the lower bound of `>=` and `~=`)
and go.mod (Go: `require` lines with a plain vX.Y.Z). Compares the dependencies of the file before and after the edit,
asks OSV (one `querybatch` call, short timeout) about the added or changed ones only, and answers from a disk cache of
(ecosystem, package, version) -> advisory IDs for 6 hours. Only package names and versions leave the machine. The caller (`hook_decision`) turns a `DependencyCheckError` into
"dependency check skipped: <reason>" and lets the edit go on.
"""
import collections
import json
import os
import re
import time

import atomic_io
import freshness
import paths

CACHE_PATH = os.path.join(paths.DATA_DIR, "dependency_advisories.json")
CACHE_TTL_S = 6 * 3600
OSV_TIMEOUT_S = 2.5
MAX_PACKAGES = 5
MAX_IDS_SHOWN = 5
TABLES = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
PREFIX = "Smart Tool dependencies: "
_SPECIFIER_RE = re.compile(r"(\^|~|>=|>|=)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?"
                           r"((?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)")
_REQUIREMENTS_FILE_RE = re.compile(r"requirements[A-Za-z0-9._-]*\.txt")
_REQUIREMENT_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
_PIP_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+!_-]*")
_GO_VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")


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


def ecosystem_of(path):
    """The OSV ecosystem of a manifest file name.

    Parameters: path (str) file path.
    Returns: "npm" for package.json, "PyPI" for requirements*.txt, "Go" for go.mod; None for any other file.
    """
    name = os.path.basename(path)
    if name == "package.json":
        return "npm"
    if name == "go.mod":
        return "Go"
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
            raise DependencyCheckError("current package.json is not valid JSON")
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


def queried_version(specifier, ecosystem="npm"):
    """The version to ask OSV about for a specifier.

    Parameters: specifier (str) the value in the dependency table; ecosystem (str) one of ECOSYSTEMS.
    Returns: str, the exact version or the lower bound of the range; None when the specifier cannot be reduced safely.
    """
    return _ECOSYSTEMS[ecosystem].version(specifier)


_Ecosystem = collections.namedtuple("_Ecosystem", "parse version label exact")
_ECOSYSTEMS = {
    "npm": _Ecosystem(_npm_pairs, _npm_version, lambda name, spec: f"{name}@{spec}", lambda spec, version: spec == version),
    "PyPI": _Ecosystem(_requirements_pairs, _pip_version, lambda name, spec: f"{name}{spec}",
                       lambda spec, version: spec == "==" + version),
    "Go": _Ecosystem(_go_pairs, _go_version, lambda name, spec: f"{name}@{spec}", lambda spec, version: spec == version),
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

    Parameters: ecosystem (str) one of ECOSYSTEMS; queries (list of (name, version)).
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
        answered = freshness.advisories_batch(ecosystem, missing, OSV_TIMEOUT_S)
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
        version = spec.version(specifier) if freshness.NAME_RES[ecosystem].fullmatch(name) else None
        if version is None:
            lines[(name, specifier)] = f"`{spec.label(name, specifier)}` specifier not verified."
        else:
            queries.append((name, specifier, version))
    problem = ""
    if queries:
        ids, problem = _advisories(ecosystem, [(name, version) for name, _specifier, version in queries])
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
