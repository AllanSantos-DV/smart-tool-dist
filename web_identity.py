"""Browser identities (User-Agent plus the headers a real browser of that family sends with it) and the log line of a
block or a rotation.

Sites that block automated reads often key on the browser identity, not on the address, so a refused request is retried
with another coherent identity. The pool lives in `web_adapters/identities.json`, which the Node adapters read too.
"""
import datetime
import json
import os
import random
import re
import sys
import threading

import paths

IDENTITIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_adapters", "identities.json")
LOG_PATH = os.path.join(paths.DATA_DIR, "web-identity.log")
LOG_MAX_BYTES = 256 * 1024
_LOG_LOCK = threading.Lock()
LOG_MARKER = "[smart-tool] web-identity "
MAX_ATTEMPTS = 3


def load_identities():
    """The pool, in file order; a missing or malformed file raises instead of falling back to one fixed identity."""
    with open(IDENTITIES_PATH, encoding="utf-8") as stream:
        identities = json.load(stream)["identities"]
    if not identities or any(not item.get("name") or not item.get("user_agent") for item in identities):
        raise ValueError(f"{IDENTITIES_PATH} must list identities with a name and a user_agent")
    return identities


def attempt_order(first=None):
    """At most MAX_ATTEMPTS distinct identities: `first` (a name) when given, then the rest shuffled."""
    pool = load_identities()
    random.shuffle(pool)
    if first:
        pool.sort(key=lambda item: item["name"] != first)
    return pool[:MAX_ATTEMPTS]


def request_headers(identity, **extra):
    """Headers of one identity for a request; `extra` overrides or adds (Accept-Language, Referer...)."""
    return {"User-Agent": identity["user_agent"], **identity.get("headers", {}), **extra}


def format_line(provider, identity, reason, outcome):
    """One log line: who was refused or served, with which identity, why, and what happened next."""
    return f"{LOG_MARKER}provider={provider} identity={identity} reason={json.dumps(reason)} outcome={outcome}\n"


def record(lines):
    """Appends the lines (strings from `format_line`) to the log file, keeping its size bounded. Never raises: a log
    that cannot be written must not cost the search it describes."""
    lines = [line for line in lines if line.startswith(LOG_MARKER)]
    if not lines:
        return
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with _LOG_LOCK:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            with open(LOG_PATH, "a", encoding="utf-8") as stream:
                stream.writelines(f"{stamp} {line[len('[smart-tool] '):]}" for line in lines)
    except OSError as exc:
        sys.stderr.write(f"[smart-tool] web-identity log not written ({exc})\n")


def lines_in(stderr_text):
    """The identity log lines inside the stderr of an adapter."""
    return [line + "\n" for line in str(stderr_text or "").splitlines() if line.startswith(LOG_MARKER)]


def summary():
    """Counts of the log file by provider: blocked and recovered per identity, and how often the identities ran out.
    An absent log is an empty table; a line that does not parse raises, naming it."""
    table = {}
    try:
        with open(LOG_PATH, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return table
    pattern = re.compile(r"web-identity provider=(\S+) identity=(\S+) reason=.* outcome=(blocked|recovered|exhausted)$")
    for line in lines:
        match = pattern.search(line)
        if not match:
            raise ValueError(f"{LOG_PATH}: unreadable line: {line[:120]}")
        provider, identity, outcome = match.groups()
        row = table.setdefault(provider, {"exhausted": 0, "identities": {}})
        if outcome == "exhausted":
            row["exhausted"] += 1
        else:
            counts = row["identities"].setdefault(identity, {"blocked": 0, "recovered": 0})
            counts[outcome] += 1
    return table
