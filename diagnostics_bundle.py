"""A redacted diagnostics bundle for a bug report: one local JSON file in the data folder's `diagnostics` subfolder.

It holds the versions, the setup audit counts, the model failures and use, the web source health and quota and the tails of
daemon.log and browser-fetch.log. Every string is scrubbed before the file is written: credentials, the user's home path
(`~`), e-mail addresses and URLs are replaced, and the search queries, the project folders and the requesters are never
collected. Nothing is sent anywhere; the file is for the user to read and attach.
"""
import datetime
import json
import os
import platform
import re
import time
import uuid

import atomic_io
import browser_fetch_state
import credential_check
import model_health
import paths
import setup_audit
import usage_meter
import version
import web_search_health
import web_search_quota

DIAGNOSTICS_DIR = os.path.join(paths.DATA_DIR, "diagnostics")
LOGS = {"daemon.log": os.path.join(paths.DATA_DIR, "daemon.log"), "browser-fetch.log": browser_fetch_state.LOG_PATH}
TAIL_LINES = 200
TAIL_BYTES = 1024 * 1024
USAGE_DAYS = 30

_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY"
                                r"(?: BLOCK)?-----|\Z)", re.S)
_SECRET_SHAPES = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "<redacted: API key>"),
    (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I), "Bearer <redacted>"),
)
_SECRET_ASSIGNMENT = re.compile(r"(\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|authorization|credential)s?"
                                r"[\\\"']*\s*[:=]\s*[\\\"']*)[^\s\"',;&]{6,}", re.I)
_OTHER_HOME = re.compile(r"[A-Za-z]:(?:\\|/)+Users(?:\\|/)+[^\\/\s\"']+|/(?:home|Users)/[^/\s\"']+", re.I)
_UNC_PATH = re.compile(r"\\\\(?:\\\\)?[^\s\"'<>:|?*\\/]+(?:\\+[^\s\"'<>:|?*\\/]+)+")
_WSL_DRIVE_PATH = re.compile(r"(?<![\w/.:~-])/mnt/[A-Za-z](?:/[^\s\"'<>:|?*]*)?")
_DRIVE_PATH = re.compile(r"[A-Za-z]:(?:\\\\|\\|/)+[^\s\"'<>:|?*]+")
_POSIX_PATH = re.compile(r"(?<![\w/.:~-])/(?:[\w.@+-]+/)+[\w.@+-]*")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>)\]}]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _home_pattern():
    """Regex of the user's home folder in the spellings a log or an error carries (backslashes, slashes, backslashes
    escaped for JSON), or None when the home is a filesystem root, which would hide every separator."""
    home = os.path.expanduser("~")
    if not home or os.path.dirname(home) == home:
        return None
    spellings = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
    return re.compile("|".join(re.escape(spelling) for spelling in sorted(spellings, key=len, reverse=True)), re.I)


def scrub_text(text):
    """`text` with the secrets and the owner's identifiers replaced by placeholders.

    Parameters: text (str). Returns: the scrubbed str. Replaced: private-key blocks and the credential shapes of
    credential_check, OpenAI-style keys, bearer tokens, the value of a `key=`/`token:`/`password=` assignment, the user's
    home path (`~`, any user's home folder too), URLs (so query strings go with them) and e-mail addresses.

    Technical doc: docs/scope/technical/_root.md#diagnostics_bundle.py::scrub_text"""
    text = _PRIVATE_KEY_BLOCK.sub("<redacted: private key>", text)
    text = credential_check.redact(text)
    for pattern, placeholder in _SECRET_SHAPES:
        text = pattern.sub(placeholder, text)
    text = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", text)
    home = _home_pattern()
    if home:
        text = home.sub("~", text)
    text = _UNC_PATH.sub("<path>", text)
    text = _WSL_DRIVE_PATH.sub("<path>", text)
    text = _OTHER_HOME.sub("~", text)
    text = _URL.sub("<url>", text)
    text = _DRIVE_PATH.sub("<path>", text)
    text = _POSIX_PATH.sub("<path>", text)
    return _EMAIL.sub("<email>", text)


def scrub(value):
    """`value` with every string in it, keys included, passed through `scrub_text`.

    Parameters: value (a JSON-shaped structure: dict, list, tuple, str, number, bool or None). Returns: the same shape.

    Technical doc: docs/scope/technical/_root.md#diagnostics_bundle.py::scrub"""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {scrub_text(key) if isinstance(key, str) else key: scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


def _tail(path):
    """The last TAIL_LINES lines of the log at `path`, whether lines were cut and the line count when the whole file was
    read. Raises FileNotFoundError when the file does not exist, OSError when it cannot be read."""
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - TAIL_BYTES))
            raw = stream.read()
    except FileNotFoundError:
        raise FileNotFoundError("log file not found") from None
    lines = raw.decode("utf-8", "replace").splitlines()
    whole = size <= TAIL_BYTES
    if not whole:
        lines = lines[1:]
    return {"lines": lines[-TAIL_LINES:], "truncated": len(lines) > TAIL_LINES or not whole,
            "total_lines": len(lines) if whole else None}


def _setup_audit():
    """The stored setup audit as counts: severity totals over all audited projects and one summary per project, without
    the project folders."""
    entries = list(setup_audit.index().values())
    totals = {}
    for entry in entries:
        for severity, count in entry["summary"].items():
            totals[severity] = totals.get(severity, 0) + count
    return {"projects_audited": len(entries), "totals": totals,
            "projects": [{key: entry.get(key) for key in ("summary", "new", "resolved", "checked_at", "trigger")}
                         for entry in entries]}


def _web():
    """Per-tier outcome counts and the keyless quota of the quota tiers: no requesters, queries or folders."""
    return {"tiers": web_search_health.by_tier(),
            "quota": {tier: web_search_quota.provider_status(tier) for tier in web_search_quota.WINDOWS}}


def _section(name, collect, unavailable):
    """`collect()`, or `{"error": "Type: message"}` with `name` added to `unavailable` when it raises: the bundle names a
    part that could not be read instead of leaving it out."""
    try:
        return collect()
    except Exception as exc:
        unavailable.append(name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def write():
    """Collects the bundle, scrubs it and writes it as one new JSON file in DIAGNOSTICS_DIR through atomic_io.

    Returns: {"path", "bytes", "unavailable" (names of the parts that could not be read, also marked inside the file),
    "note"}. Nothing is sent anywhere.

    Technical doc: docs/scope/technical/_root.md#diagnostics_bundle.py::write"""
    unavailable = []
    data = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "redaction": ["credentials, private keys, bearer tokens and secret assignments replaced",
                      "home folder replaced by ~, e-mail addresses and URLs replaced",
                      "not collected: search queries, project folders, requesters",
                      "stays on this machine: Smart Tool sends nothing"],
        "versions": {"smart_tool": version.VERSION, "python": platform.python_version(),
                     "platform": platform.platform()},
        "setup_audit": _section("setup_audit", _setup_audit, unavailable),
        "models": _section("models", model_health.summary, unavailable),
        "usage": _section("usage", lambda: usage_meter.overview(USAGE_DAYS), unavailable),
        "web": _section("web", _web, unavailable),
        "logs": {name: _section(f"logs.{name}", lambda path=path: _tail(path), unavailable)
                 for name, path in LOGS.items()},
    }
    text = json.dumps(scrub(data), indent=1, ensure_ascii=False)
    os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)
    path = os.path.join(DIAGNOSTICS_DIR, f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json")
    atomic_io.write_text(path, text)
    return {"path": path, "bytes": len(text.encode("utf-8")), "unavailable": unavailable,
            "note": "The file stays on this machine and nothing was sent anywhere. Read it, then attach it to the bug "
                    "report yourself."}
