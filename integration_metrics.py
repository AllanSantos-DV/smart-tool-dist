import collections
import json
import os
import paths
import threading
import time

import atomic_io
import router

PATH = os.path.join(paths.DATA_DIR, "integration-metrics.jsonl")
MAX_BYTES = 20 * 1024 * 1024
_LOCK = threading.Lock()


def record(event, **fields):
    line = json.dumps({"ts": time.time(), "event": event, **fields}, ensure_ascii=False)
    with _LOCK:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        atomic_io.rotate(PATH, MAX_BYTES)
        with open(PATH, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def _read(path, since):
    rows = []
    for candidate in (path + ".1", path):
        try:
            with open(candidate, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)) and row["ts"] >= since:
                        rows.append(row)
        except FileNotFoundError:
            continue
    return rows


def summary(days=7):
    since = time.time() - days * 86400
    events = _read(PATH, since)
    by_client = collections.defaultdict(collections.Counter)
    reasons, rejects = collections.Counter(), collections.Counter()
    for row in events:
        by_client[row.get("client") or "desconhecido"][row.get("event", "?")] += 1
        if row.get("event") == "search" and row.get("single_language_reason"):
            reasons[row["single_language_reason"][:120]] += 1
        if row.get("event") == "reject":
            rejects[row.get("kind", "outro")] += 1
    web = [row for row in events if row.get("event") == "web_search"]
    origin = lambda row: "error" if row.get("error") else row.get("cache", "?")
    by_cache = collections.Counter(origin(row) for row in web)
    elapsed = collections.defaultdict(list)
    for row in web:
        if isinstance(row.get("elapsed_s"), (int, float)):
            elapsed[origin(row)].append(row["elapsed_s"])
    reused = sum(count for status, count in by_cache.items() if status not in ("fresh", "error"))
    web_cache = {
        "calls": len(web), "reused": reused, "reuse_rate": round(reused / len(web), 3) if web else None,
        "by_cache": dict(by_cache),
        "probes": sum(1 for row in web if row.get("probe")),
        "probe_verdicts": dict(collections.Counter(row["verdict"] for row in web if row.get("verdict"))),
        "errors": dict(collections.Counter(row["error"] for row in web if row.get("error"))),
        "reused_volatility": dict(collections.Counter(row.get("volatility") or "?" for row in web
                                                      if origin(row) not in ("fresh", "error"))),
        "bilingual": sum(1 for row in web if row.get("bilingual")),
        "degraded": sum(1 for row in web if row.get("degraded")),
        "with_warnings": sum(1 for row in web if row.get("warnings")),
        "avg_elapsed_s": {status: round(sum(values) / len(values), 2) for status, values in elapsed.items()},
    }
    routed = _read(router.METRICS_PATH, since)
    decisions = collections.defaultdict(collections.Counter)
    for row in routed:
        decisions[row.get("client") or "no client"][row.get("decision", "?")] += 1
    return {"days": days, "events_by_client": {k: dict(v) for k, v in by_client.items()},
            "rejects_by_kind": dict(rejects), "single_language_reasons": dict(reasons.most_common(20)),
            "hook_decisions_by_client": {k: dict(v) for k, v in decisions.items()},
            "web_search_cache": web_cache,
            "files": {"integration": PATH, "router": router.METRICS_PATH}}
