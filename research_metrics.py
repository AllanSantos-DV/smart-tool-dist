#!/usr/bin/env python3
"""Trilha de calibração da memória (cosseno + rerank) e do teto de pesquisa:
`site_memory_similarity_threshold` pré-filtra tópicos, o reranker confirma o
reaproveitamento, e `research_max_iterations` limita o retorno por rodada. Write-only, como
`router-metrics.jsonl` — nada no daemon lê isto de volta; quem lê é
`.token-guard/analyze_research.py`.

Grava só o que a calibração usa: textos de consulta/tópico truncados e números. Nunca
snippets nem páginas, que são conteúdo de terceiros e não ajudam a decidir o valor.
"""
import json
import os
import paths
import threading
import time

import atomic_io

METRICS_PATH = os.path.join(paths.DATA_DIR, "research-metrics.jsonl")
METRICS_MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 200
MAX_PAIRS = 10

# O daemon é multi-thread (fan-out síncrono e jobs de pesquisa em pools próprios): sem
# lock, dois appends grandes podem se intercalar na mesma linha.
_LOCK = threading.Lock()


def _short(text):
    text = str(text or "")
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS] + "..."
    return text


def _append(record):
    record = {"ts": time.time(), **record}
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
            atomic_io.rotate(METRICS_PATH, METRICS_MAX_BYTES)
            with open(METRICS_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except (OSError, TypeError, ValueError):
        # Métrica é para calibrar depois: disco cheio ou arquivo travado não pode derrubar
        # uma pesquisa.
        pass


def log_site_memory(query, threshold, scored_topics, recalled, rerank_scores=None, rerank_threshold=None):
    """Uma correlação da memória do dia. `scored_topics`: `[(tópico, similaridade)]` de
    todos os tópicos comparados — só os `MAX_PAIRS` mais altos vão pro disco, que são os
    que decidem onde o limiar corta."""
    try:
        ranked = sorted(scored_topics, key=lambda pair: pair[1], reverse=True)
        record = {
            "kind": "site_memory",
            "query": _short(query),
            "threshold": threshold,
            "topics": len(ranked),
            "matched": sum(1 for _, score in ranked if score >= threshold),
            "recalled": recalled,
            "pairs": [
                {
                    "topic": _short(topic), "score": round(float(score), 4),
                    **({"rerank_score": round(float(rerank_scores[topic]), 4)}
                       if rerank_scores is not None and topic in rerank_scores else {}),
                }
                for topic, score in ranked[:MAX_PAIRS]
            ],
        }
        if rerank_scores is not None:
            record["rerank_threshold"] = rerank_threshold
            record["rerank_matched"] = sum(
                score >= rerank_threshold for score in rerank_scores.values()
            )
    except Exception:
        return
    _append(record)


def log_research_job(topic, max_iterations, rounds, stopped_by, sources, elapsed_s):
    """Um job `depth='research'` concluído. `rounds` já traz, por rodada, quanto ela
    acrescentou (`new_urls`, `findings`) — é a curva de retorno marginal que decide o
    teto."""
    try:
        record = {
            "kind": "research",
            "topic": _short(topic),
            "max_iterations": max_iterations,
            "stopped_by": stopped_by,
            "sources": sources,
            "elapsed_s": round(elapsed_s, 1),
            "rounds": [
                {
                    "query": _short(r.get("query")),
                    "results": r.get("results_count", 0),
                    "new_urls": r.get("new_urls", 0),
                    "findings": r.get("findings", 0),
                    "gaps": r.get("gaps", 0),
                    "continue": bool(r.get("continue")),
                    "reason": _short(r.get("reason")),
                }
                for r in rounds
            ],
        }
    except Exception:
        return
    _append(record)
