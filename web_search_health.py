#!/usr/bin/env python3
"""Histórico de sucesso/falha por Tier do web_search, lido de volta pelo router_model
antes de cada decisão — diferente de router-metrics.jsonl (router.py), que é write-only
e serve só de audit trail para o roteamento de smart_search.
"""
import json
import os
import paths
import threading
import time

HEALTH_PATH = os.path.join(paths.DATA_DIR, "web-search-health.jsonl")
DEFAULT_WINDOW = 20
_MAX_LINES_SCANNED = 2000
_MAX_LINES_BEFORE_ROTATE = 5000

# Todo record()/rotação passa por aqui: o daemon é ThreadingHTTPServer, então
# múltiplas requisições concorrentes podem chamar record() na mesma linha de
# tempo — sem lock, escritas de threads diferentes se intercalam no mesmo
# arquivo e corrompem linhas JSONL.
_LOCK = threading.Lock()


OUTCOMES = ("ok", "failed", "rate_limited", "captcha", "paused")
BREAKER_STREAK = 5
BREAKER_RECOVERY_S = 600


def record(tier_name, success, outcome=None, requester=None):
    """One tier call: outcome tells a provider 429 (rate_limited), a captcha and a self-imposed quota pause (paused,
    left out of success rates) apart from an ordinary failure, so quota pressure can be read from the log. requester
    (requester.claim) says which session asked, never what."""
    outcome = outcome or ("ok" if success else "failed")
    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown web search outcome: {outcome}")
    os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
    entry = {"ts": time.time(), "tier": tier_name, "success": bool(success), "outcome": outcome}
    if requester:
        entry["requester"] = requester
    with _LOCK:
        with open(HEALTH_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_if_needed()


def _rotate_if_needed():
    # Já dentro de _LOCK (chamado só por record()). Mantém o arquivo limitado
    # a _MAX_LINES_SCANNED linhas — sem isso, cada leitura futura reprocessa o
    # arquivo inteiro por tempo indefinido de uso real.
    try:
        with open(HEALTH_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    if len(lines) <= _MAX_LINES_BEFORE_ROTATE:
        return
    tmp_path = HEALTH_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines[-_MAX_LINES_SCANNED:])
    os.replace(tmp_path, HEALTH_PATH)  # atômico — um crash a meio da rotação não zera o arquivo real


def _read_recent_records():
    with _LOCK:
        try:
            with open(HEALTH_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
    records = []
    for line in lines[-_MAX_LINES_SCANNED:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def by_requester():
    """The recent web search calls read back as who asked: one row per client and project folder.

    Paused calls are not calls and are left out. The log never holds the query text.
    Returns: dict {"rows": [{"client", "project", "sessions", "calls", "failed", "last_at", "sources"}], "calls": int,
    "unattributed": int}; rows are busiest first, `sources` says how far the attribution got (see `requester`), and
    `unattributed` counts the calls recorded without a requester.
    """
    groups, unattributed, calls = {}, 0, 0
    for record in _read_recent_records():
        if record.get("outcome") == "paused":
            continue
        calls += 1
        who = record.get("requester")
        if not isinstance(who, dict):
            unattributed += 1
            continue
        row = groups.setdefault((who.get("client") or "unknown", who.get("project") or ""),
                                {"sessions": set(), "calls": 0, "failed": 0, "last_at": 0, "sources": set()})
        row["calls"] += 1
        row["failed"] += not record.get("success")
        row["last_at"] = max(row["last_at"], record.get("ts") or 0)
        row["sessions"].add(who.get("session") or who.get("mcp_session") or "")
        row["sources"].add(who.get("source") or "none")
    rows = [{"client": client, "project": project, "sessions": len(row["sessions"]), "calls": row["calls"],
             "failed": row["failed"], "last_at": row["last_at"], "sources": sorted(row["sources"])}
            for (client, project), row in groups.items()]
    rows.sort(key=lambda row: (-row["calls"], -row["last_at"]))
    return {"rows": rows, "calls": calls, "unattributed": unattributed}


def recent_success_rate(tier_name, window=DEFAULT_WINDOW):
    records = [r for r in _read_recent_records() if r.get("tier") == tier_name and r.get("outcome") != "paused"]
    if not records:
        return None
    records = records[-window:]
    successes = sum(1 for r in records if r.get("success"))
    return successes / len(records)


def summary_for_tiers(tier_names, window=DEFAULT_WINDOW):
    all_records = _read_recent_records()
    summary = {}
    for name in tier_names:
        records = [r for r in all_records if r.get("tier") == name and r.get("outcome") != "paused"][-window:]
        if not records:
            summary[name] = None
        else:
            successes = sum(1 for r in records if r.get("success"))
            summary[name] = successes / len(records)
    return summary


def tripped(tier_names, now=None, streak=BREAKER_STREAK, recovery_s=BREAKER_RECOVERY_S):
    """The tiers that keep failing: `streak` calls in a row ended in a plain failure and the last one is less than
    `recovery_s` ago. Read from the same log as everything else, so there is no state to reset: once the window has
    passed the tier is called again, and one more failure starts another window while a success clears the streak.
    A pause does not count and does not break the streak; a success, a rate limit or a captcha breaks it (those have
    their own pauses).
    Returns: dict tier -> {"streak": int, "retry_in_s": float} for the tripped tiers only.
    """
    now = time.time() if now is None else now
    records = _read_recent_records()
    result = {}
    for name in tier_names:
        count, last_ts = 0, None
        for entry in reversed(records):
            if entry.get("tier") != name or entry.get("outcome") == "paused":
                continue
            if (entry.get("outcome") or ("ok" if entry.get("success") else "failed")) != "failed":
                break
            last_ts = last_ts or entry.get("ts")
            count += 1
        if count >= streak and last_ts is not None and now - last_ts < recovery_s:
            result[name] = {"streak": count, "retry_in_s": recovery_s - (now - last_ts)}
    return result


def by_tier():
    """The recent web search calls counted per tier: one count per outcome and the success rate.

    The rate is ok calls over the calls that were made, so a self-imposed pause (paused) is not a miss. A tier with
    only paused calls has no rate. A record with an outcome `record` would not write fails loud.
    Returns: dict tier -> {"ok", "failed", "rate_limited", "captcha", "paused", "success_rate"}.
    """
    counts = {}
    for entry in _read_recent_records():
        outcome = entry.get("outcome") or ("ok" if entry.get("success") else "failed")
        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown web search outcome in {HEALTH_PATH}: {outcome}")
        counts.setdefault(entry.get("tier"), dict.fromkeys(OUTCOMES, 0))[outcome] += 1
    table = {}
    for tier in sorted(counts, key=str):
        row = counts[tier]
        made = sum(row.values()) - row["paused"]
        table[tier] = {**row, "success_rate": round(row["ok"] / made, 3) if made else None}
    return table
