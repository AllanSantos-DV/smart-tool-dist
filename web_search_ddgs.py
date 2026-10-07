#!/usr/bin/env python3
"""Adaptador da biblioteca ddgs: um backend por chamada, sem chave."""
import json
import sys

from ddgs import DDGS
from ddgs.exceptions import DDGSException

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


def main():
    query, num, backend, timeout = parse_args(sys.argv[1:])
    try:
        rows = DDGS(timeout=timeout).text(query, max_results=num, backend=backend)
    except DDGSException as exc:
        print(f"{backend}: {exc}", file=sys.stderr)
        return 1
    results = [{"title": row.get("title", ""), "url": row.get("href", ""), "snippet": row.get("body", "")}
               for row in rows if row.get("href")]
    print(json.dumps(results[:num], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
