import collections
import json
import os
import paths
import re
import threading
import time

import atomic_io
import router

PATH = os.path.join(paths.DATA_DIR, "integration-metrics.jsonl")
MAX_BYTES = 20 * 1024 * 1024
_LOCK = threading.Lock()
FOLLOW_WINDOW_S = 900
# Measured on 2026-10-12 with docs/spikes/bash_bypass_replay.py over 3 days of Claude Code transcripts (14,580 Grep and
# shell calls), at ~4 characters per token: the 310 calls the rule redirects put 2,031 characters on average in the
# context when they ran; a smart_search round trip (pending reply 716 + result 7,924) puts 8,640.
NATIVE_REDIRECTED_TOKENS = 510
SMART_SEARCH_TOKENS = 2160


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
            "redirects": redirects(days),
            "web_search_cache": web_cache,
            "files": {"integration": PATH, "router": router.METRICS_PATH}}


def _next_in_session(rows, index):
    """The session's next logged call within FOLLOW_WINDOW_S of rows[index], or None."""
    start = rows[index]["ts"]
    return next((row for row in rows[index + 1:] if row["ts"] - start <= FOLLOW_WINDOW_S), None)


def redirects(days=7):
    """Redirects of native searches in the last days and what came next in the same session: the Smart Tool answer
    returned as the call's output (served), a smart_search call (followed), an allowed native call such as reading a
    file it found (native), another redirect (blocked_again), or no other
    call within FOLLOW_WINDOW_S. Redirects logged without a session (before 0.9.15, or a client that sends none) count
    as unknown. tokens_per_call holds the measured context of each side, so the panel never claims a saving it did
    not measure."""
    rows = sorted(_read(router.METRICS_PATH, time.time() - days * 86400), key=lambda row: row["ts"])
    sessions = collections.defaultdict(list)
    for row in rows:
        if row.get("session"):
            sessions[row["session"]].append(row)
    by_tool, by_reason, outcomes = collections.Counter(), collections.Counter(), collections.Counter()
    for row in rows:
        if row.get("decision") != "redirect":
            continue
        by_tool[row.get("tool_name") or "?"] += 1
        by_reason[re.sub(r"\d+", "N", str(row.get("reason") or "?"))[:80]] += 1
        own = sessions.get(row.get("session"))
        if not own:
            outcomes["unknown"] += 1
            continue
        after = _next_in_session(own, own.index(row))
        if after is None:
            outcomes["no_other_call"] += 1
        elif after.get("decision") == "served":
            outcomes["served"] += 1
        elif str(after.get("tool_name", "")).startswith("mcp__smart-tool__"):
            outcomes["followed"] += 1
        elif after.get("decision") == "redirect":
            outcomes["blocked_again"] += 1
        else:
            outcomes["native"] += 1
    return {"days": days, "total": sum(by_tool.values()), "by_tool": dict(by_tool),
            "by_reason": dict(by_reason.most_common(8)), "outcomes": dict(outcomes),
            "tokens_per_call": {"native_redirected": NATIVE_REDIRECTED_TOKENS, "smart_search": SMART_SEARCH_TOKENS,
                                "measured": "2026-10-12, 3 days of Claude Code transcripts"}}
