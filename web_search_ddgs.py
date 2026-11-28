#!/usr/bin/env python3
"""Adaptador da biblioteca ddgs: um backend por chamada, sem chave."""
import json
import re
import sys
import time

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException

import web_identity

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv):
    # Manual parsing: the query is positional and may start with "-" (e.g. "-v flag in pytest").
    options, query_parts, i = {"--num": "8", "--backend": None, "--timeout": "10"}, [], 0
    while i < len(argv):
        if argv[i] in options and i + 1 < len(argv):
            options[argv[i]] = argv[i + 1]
            i += 2
        else:
            query_parts.append(argv[i])
            i += 1
    if not query_parts or not options["--backend"]:
        raise ValueError('Usage: python web_search_ddgs.py "query" --backend name [--num 8] [--timeout 10]')
    return " ".join(query_parts), int(options["--num"]), options["--backend"], int(options["--timeout"])


_BLOCK_SIGNAL = re.compile(r"\b(?:202|403|429)\b|anomaly|captcha|rate.?limit", re.IGNORECASE)
RETRY_DELAY_S = 0.4
TOTAL_BUDGET_S = 12


def _block_reason(exc):
    """The block signal found in the library's message, or the exception class: the full message can carry the request
    URL with the search terms, which must not reach the persisted log."""
    found = _BLOCK_SIGNAL.search(str(exc))
    return found.group(0) if found else type(exc).__name__


def _is_block(exc):
    return isinstance(exc, RatelimitException) or bool(_BLOCK_SIGNAL.search(str(exc)))


def search(query, num, backend, timeout):
    """Rows of one backend. A block signal is retried with a fresh client, which the library gives another random
    browser profile (TLS, HTTP/2 and headers together); other failures raise at once."""
    tried = []
    deadline = time.monotonic() + TOTAL_BUDGET_S
    for attempt in range(web_identity.MAX_ATTEMPTS):
        identity = f"ddgs-profile-{attempt + 1}"
        remaining = deadline - time.monotonic()
        if tried and remaining < 2:
            break
        try:
            rows = DDGS(timeout=max(1, min(timeout, int(remaining)))).text(query, max_results=num, backend=backend)
        except DDGSException as exc:
            if not _is_block(exc):
                raise
            tried.append(identity)
            sys.stderr.write(web_identity.format_line(backend, identity, _block_reason(exc), "blocked"))
            if attempt + 1 < web_identity.MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_S)
            last = exc
            continue
        if tried:
            sys.stderr.write(web_identity.format_line(backend, identity, f"after {', '.join(tried)} blocked", "recovered"))
        return rows
    sys.stderr.write(web_identity.format_line(backend, tried[-1], f"all {len(tried)} profiles blocked", "exhausted"))
    raise last


def main():
    query, num, backend, timeout = parse_args(sys.argv[1:])
    try:
        rows = search(query, num, backend, timeout)
    except DDGSException as exc:
        print(f"{backend}: {exc}", file=sys.stderr)
        return 1
    results = [{"title": row.get("title", ""), "url": row.get("href", ""), "snippet": row.get("body", "")}
               for row in rows if row.get("href")]
    print(json.dumps(results[:num], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
