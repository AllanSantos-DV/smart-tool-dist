"""Vulnerable-dependency notice for an edit of package.json, built before the edit runs.

Compares the dependency tables of the file before and after the edit, asks OSV (one `querybatch` call, short timeout)
about the added or changed ones only, and answers from a disk cache of (package, version) -> advisory IDs for 6 hours.
Only package names and versions leave the machine. The caller (`hook_decision`) turns a `DependencyCheckError` into
"dependency check skipped: <reason>" and lets the edit go on.
"""
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


class DependencyCheckError(RuntimeError):
    """The check could not run (OSV unreachable or answering badly, unreadable cache, current file not JSON)."""


def _tables(text):
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


def changed_dependencies(before, after):
    """Dependencies added or whose specifier changed between two package.json texts.

    Parameters: before (str or None) text before the edit, None when the file did not exist; after (str) text after.
    Returns: list of (name, specifier); empty when `after` is not valid JSON (the user is mid-edit).
    Raises: DependencyCheckError when `before` exists but is not valid JSON.
    """
    now = _tables(after)
    if now is None:
        return []
    if before is None or not before.strip():
        old = {}
    else:
        old = _tables(before)
        if old is None:
            raise DependencyCheckError("current package.json is not valid JSON")
    return [pair for pair in now if pair not in old]


def queried_version(specifier):
    """The version to ask OSV about for a specifier.

    Parameters: specifier (str) the value in the dependency table.
    Returns: str, the exact version or the lower bound of a ^, ~, >= or > range; None when the specifier cannot be
    reduced safely (tags, `*`, URLs, file:, git, workspace:, npm: aliases, x-ranges, compound ranges).
    """
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


def _advisories(queries):
    """Advisory IDs for (name, version) pairs, from the cache and one OSV batch for the rest.

    Parameters: queries (list of (name, version)).
    Returns: tuple (list of IDs or None per query in order, str cache-write problem or "").
    Raises: DependencyCheckError when the cache is corrupt or OSV cannot answer.
    """
    cache, now = _load_cache(), time.time()
    found = {query: _fresh_ids(cache.get("@".join(query)), now) for query in queries}
    missing = [query for query in queries if found[query] is None]
    if not missing:
        return [found[query] for query in queries], ""
    try:
        answered = freshness.npm_advisories_batch(missing, OSV_TIMEOUT_S)
    except RuntimeError as exc:
        raise DependencyCheckError(str(exc)) from exc
    for query, ids in zip(missing, answered):
        found[query] = ids
        if ids is not None:
            cache["@".join(query)] = {"ids": ids, "at": now}
    cache = {key: entry for key, entry in cache.items() if _fresh_ids(entry, now) is not None}
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        atomic_io.write_secret_text(CACHE_PATH, json.dumps(cache, indent=1))
    except OSError as exc:
        return [found[query] for query in queries], f"advisory cache not saved: {exc}"
    return [found[query] for query in queries], ""


def notice(before, after):
    """The dependency notice for one package.json edit.

    Parameters: before (str or None) file text before the edit; after (str) text after it.
    Returns: str, "Smart Tool dependencies: ..." lines for the changed dependencies that have advisories or could not
    be verified, "" when there is nothing to say.
    Raises: DependencyCheckError when the check could not run.
    """
    changed = changed_dependencies(before, after)
    handled, extra = changed[:MAX_PACKAGES], len(changed) - MAX_PACKAGES
    lines, queries = {}, []
    for name, specifier in handled:
        version = queried_version(specifier) if freshness.NPM_NAME_RE.fullmatch(name) else None
        if version is None:
            lines[(name, specifier)] = f"`{name}@{specifier}` specifier not verified."
        else:
            queries.append((name, specifier, version))
    problem = ""
    if queries:
        ids, problem = _advisories([(name, version) for name, _specifier, version in queries])
        for (name, specifier, version), advisories in zip(queries, ids):
            label = f"`{name}@{specifier}`" + ("" if version == specifier else f" (checked {version})")
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
