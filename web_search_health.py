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


def record(tier_name, success, outcome=None):
    """One tier call: outcome tells a provider 429 (rate_limited), a captcha and a self-imposed quota pause (paused,
    left out of success rates) apart from an ordinary failure, so quota pressure can be read from the log."""
    outcome = outcome or ("ok" if success else "failed")
    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown web search outcome: {outcome}")
    os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
    entry = {"ts": time.time(), "tier": tier_name, "success": bool(success), "outcome": outcome}
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
