#!/usr/bin/env python3
"""Adaptador HTTP dos provedores com índice próprio: plano gratuito sem chave, chave opcional do usuário.

Mesmo padrão validado por Hermes Agent (plugins/web/keyless_mcp.py) e OpenCode (core/src/tool/websearch.ts):
o endpoint público responde sem credencial; a chave do usuário entra como cabeçalho na mesma chamada.
"""
import argparse
import datetime
import email.utils
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import config

# Shown in the setup screen: where the key comes from and what it changes. Sources accessed 2026-10-03.
PROVIDERS = {
    "exa": {"label": "Exa", "signup": "https://dashboard.exa.ai/api-keys", "key_prefix": "",
            "free": "No key: Exa public MCP, up to 3 searches/s and 150 calls per day (each bilingual search uses 2).",
            "keyed": "With key: your account limits and credits (new accounts start with free credits)."},
    "parallel": {"label": "Parallel", "signup": "https://platform.parallel.ai", "key_prefix": "",
                 "free": "No key: Parallel public MCP; measured ~60 searches per hour before a 1 h pause.",
                 "keyed": "With key: your account quota (600 searches/min on the default plan)."},
    "keenable": {"label": "Keenable", "signup": "https://app.keenable.ai/signup", "key_prefix": "keen_",
                 "free": "No key: Keenable public endpoint (own index and ranking), rate limited.",
                 "keyed": "With key: 100k free requests on signup, then US$ 4 per thousand."},
    "tavily": {"label": "Tavily", "signup": "https://app.tavily.com", "key_prefix": "tvly-",
               "free": "No key: Tavily keyless mode, rate limited.",
               "keyed": "With key: 1,000 free credits per month, no card."},
    "firecrawl": {"label": "Firecrawl", "signup": "https://www.firecrawl.dev/app/api-keys", "key_prefix": "fc-",
                  "free": "No key: Firecrawl public search, rate limited.",
                  "keyed": "With key: your account credits and limits."},
}
_RATE_LIMIT_MARKERS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "quota exceeded", "slow down",
                       "limit exceeded", "limit reached", "exhausted")
_RATE_LIMIT_CODE = re.compile(r"(error|http|status|code)\W{0,3}429\b")
_AUTH_MARKERS = ("invalid api key", "unauthorized", "authentication failed", "malformed api key", "invalid token")
# Rate limits signalled inside an MCP result carry no Retry-After; pause briefly instead of the 5 min default.
MCP_RATE_LIMIT_PAUSE_S = 60
_SESSION_ID = uuid.uuid4().hex


class ProviderRateLimited(Exception):
    """429 persistente; o daemon deve abrir o circuito deste provedor."""

    def __init__(self, retry_after):
        self.retry_after = min(3600, max(1, int(retry_after)))
        super().__init__("quota temporarily unavailable")


class ProviderAuthError(Exception):
    """O provedor recusou a chave informada."""


class ProviderError(Exception):
    """Resposta do provedor fora do contrato; `search` troca a chave por *** antes de propagar."""


def _retry_after_seconds(headers):
    raw = (headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0, math.ceil(float(raw)))
    except ValueError:
        try:
            instant = email.utils.parsedate_to_datetime(raw)
            return max(0, math.ceil((instant - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _post(url, body, headers, timeout=7):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "User-Agent": "smart-tool", **headers})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                exc.close()
                raise ProviderAuthError(f"HTTP {exc.code}") from None
            if exc.code != 429:
                raise
            retry_after = _retry_after_seconds(exc.headers)
            exc.close()
            # One short retry recovers transient limits; a long wait opens the daemon circuit instead.
            if attempt == 0 and (retry_after is None or retry_after <= 2):
                time.sleep(max(0.5, retry_after or 0))
                continue
            raise ProviderRateLimited(retry_after or 300) from None


def _sse_payloads(body):
    events, current = [], []
    for line in re.split(r"\r\n|\r|\n", body):
        if line.startswith("data:"):
            current.append(line[5:].lstrip(" "))
        elif not line.strip() and current:
            events.append("\n".join(current))
            current = []
    if current:
        events.append("\n".join(current))
    return events


def _mcp_text(url, tool, arguments, headers):
    body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}, headers)
    stripped = body.strip()
    for candidate in ([stripped] if stripped.startswith("{") else []) + _sse_payloads(body):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        error, result = data.get("error"), data.get("result")
        result = result if isinstance(result, dict) else {}
        texts = [str(c.get("text", "")) for c in result.get("content") or [] if isinstance(c, dict)]
        if error or result.get("isError"):
            if error:
                message = str(error.get("message") or error) if isinstance(error, dict) else str(error)
            else:
                message = " ".join(texts)
            lowered = message.lower()
            if any(marker in lowered for marker in _RATE_LIMIT_MARKERS) or _RATE_LIMIT_CODE.search(lowered):
                raise ProviderRateLimited(MCP_RATE_LIMIT_PAUSE_S)
            if headers and any(marker in lowered for marker in _AUTH_MARKERS):
                raise ProviderAuthError("key rejected by the provider")
            raise ProviderError(message[:300] or "MCP tool failed")
        if any(texts):
            return next(t for t in texts if t)
    raise ProviderError("MCP response without text content")


def _exa(query, num, key):
    text = _mcp_text("https://mcp.exa.ai/mcp", "web_search_exa", {"query": query, "numResults": num},
                     {"x-api-key": key} if key else {})
    rows = []
    for block in text.split("\n---\n"):
        title = url = ""
        snippet, in_highlights = [], False
        for line in map(str.strip, block.splitlines()):
            if line.startswith("Title:"):
                title = "" if line[6:].strip() == "N/A" else line[6:].strip()
            elif line.startswith("URL:"):
                url = line[4:].strip()
            elif in_highlights and line and not line.startswith(("Title:", "URL:", "Published:", "Author:")):
                snippet.append(line)
            if line.startswith(("Title:", "URL:", "Highlights:", "Published:", "Author:")):
                in_highlights = line.startswith("Highlights:")
        if url:
            rows.append({"title": title, "url": url, "content": " ".join(snippet)})
    return rows


def _parallel(query, num, key):
    text = _mcp_text("https://search.parallel.ai/mcp", "web_search",
                     {"objective": query, "search_queries": [query], "session_id": _SESSION_ID},
                     {"Authorization": "Bearer " + key} if key else {})
    try:
        results = json.loads(text).get("results") or []
    except (ValueError, AttributeError):
        raise ProviderError("Parallel returned results in an unexpected format.") from None
    return [{"title": r.get("title"), "url": r.get("url"), "content": " ".join(r.get("excerpts") or [])}
            for r in results if isinstance(r, dict)]


def _keenable(query, num, key):
    if key:
        url, headers = "https://api.keenable.ai/v1/search", {"X-API-Key": key}
    else:
        url, headers = "https://api.keenable.ai/v1/search/public", {"X-Keenable-Title": "smart-tool"}
    try:
        return json.loads(_post(url, {"query": query, "max_results": num}, headers)).get("results") or []
    except (ValueError, AttributeError):
        raise ProviderError("Keenable returned results in an unexpected format.") from None


def _tavily(query, num, key):
    headers = {"Authorization": "Bearer " + key} if key else {"X-Tavily-Access-Mode": "keyless"}
    payload = json.loads(_post("https://api.tavily.com/search",
                               {"query": query, "max_results": num, "search_depth": "basic"}, headers, timeout=7))
    return payload.get("results") or []


def _firecrawl(query, num, key):
    headers = {"Authorization": "Bearer " + key} if key else {}
    payload = json.loads(_post("https://api.firecrawl.dev/v2/search", {"query": query, "limit": num}, headers,
                               timeout=7))
    if payload.get("success") is not True:
        raise ProviderError("Firecrawl did not confirm the search.")
    return (payload.get("data") or {}).get("web") or []


_SEARCH = {"exa": _exa, "parallel": _parallel, "keenable": _keenable, "tavily": _tavily, "firecrawl": _firecrawl}


def search(provider, query, num, key=None):
    if provider not in _SEARCH:
        raise ValueError("Unknown web provider.")
    key = config.get_provider_api_key(provider) if key is None else key
    try:
        rows = _SEARCH[provider](query, num, key)
    except ProviderAuthError:
        raise ProviderAuthError("key rejected by the provider") from None
    except (ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
        message = f"{type(exc).__name__}: {exc}"[:300]
        raise ProviderError(message.replace(key, "***") if key else message) from None
    if not isinstance(rows, list):
        raise ProviderError("Provider returned results in an unexpected format.")
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = str(row.get("url") or "")
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        results.append({
            "title": str(row.get("title") or "")[:300],
            "url": link,
            "snippet": str(row.get("content") or row.get("description") or row.get("snippet") or "")[:600],
        })
    return results[:num]


def validate_key(provider, key):
    """Real search with the given key: raises ProviderAuthError when the vendor rejects it.

    Measured 2026-10-03: all five vendors reject an invalid key (HTTP 401, or Exa's MCP 'error (401)')."""
    config.check_provider_key_format(key)
    prefix = PROVIDERS[provider]["key_prefix"]
    if prefix and not key.startswith(prefix):
        raise ProviderAuthError(f"{PROVIDERS[provider]['label']} keys start with {prefix}")
    return len(search(provider, "smart tool web search", 1, key=key))


def parse_args(argv):
    # Manual parsing: the query is positional and may start with "-" (e.g. "-v flag in pytest").
    options, query_parts, i = {"--num": "8", "--provider": None}, [], 0
    while i < len(argv):
        if argv[i] in options and i + 1 < len(argv):
            options[argv[i]] = argv[i + 1]
            i += 2
        else:
            query_parts.append(argv[i])
            i += 1
    if not query_parts or options["--provider"] not in config.WEB_PROVIDER_NAMES:
        raise ValueError('Usage: python web_search_external.py "query" --provider '
                         + "|".join(config.WEB_PROVIDER_NAMES) + " [--num 8]")
    return argparse.Namespace(query=" ".join(query_parts), num=int(options["--num"]), provider=options["--provider"])


def main():
    args = parse_args(sys.argv[1:])
    try:
        results = search(args.provider, args.query, args.num)
    except ProviderRateLimited as exc:
        print(f"{args.provider}: HTTP 429 retry_after={exc.retry_after}", file=sys.stderr)
        return 75
    except (ProviderAuthError, ProviderError) as exc:
        print(f"{args.provider}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"{args.provider}: HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:
        # The Authorization header never reaches tracebacks or remote error bodies.
        print(f"{args.provider}: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
