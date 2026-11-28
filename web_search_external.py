#!/usr/bin/env python3
"""Adaptador HTTP dos provedores com índice próprio: plano gratuito sem chave, chave opcional do usuário.

Mesmo padrão validado por Hermes Agent (plugins/web/keyless_mcp.py) e OpenCode (core/src/tool/websearch.ts):
o endpoint público responde sem credencial; a chave do usuário entra como cabeçalho na mesma chamada.
"""
import argparse
import contextvars
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
import web_identity

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
    "brave": {"label": "Brave", "signup": "https://api-dashboard.search.brave.com/register", "key_prefix": "",
              "free": "Needs your own key: Brave has no keyless plan.",
              "keyed": "With key: US$ 5 of credit each month (about 1,000 searches); Brave asks for a card."},
    "mojeek": {"label": "Mojeek", "signup": "https://www.mojeek.com/services/search/web-search-api/", "key_prefix": "",
               "free": "Needs your own key: the Mojeek API is paid, with no keyless plan.",
               "keyed": "With key: pay as you go, GBP 2 per 1,000 searches."},
    "searxng": {"label": "SearXNG", "signup": "https://docs.searxng.org/admin/installation.html",
                "key_prefix": "http",
                "free": "Needs your own instance: paste its address (https://host) where the key goes; its settings "
                        "must allow format=json.",
                "keyed": "With an instance: its own engines and limits; public instances usually refuse bots."},
    "marginalia": {"label": "Marginalia", "signup": "https://about.marginalia-search.com/article/api/",
                   "key_prefix": "",
                   "free": "No key: Marginalia's shared public key, often rate limited; results are CC-BY-NC-SA 4.0 "
                           "(non-commercial use).",
                   "keyed": "With key: a free non-commercial key by e-mail to Marginalia, same license."},
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
VALIDATION_QUERY = "smart tool web search"


class ProviderRateLimited(Exception):
    """429 persistente; o daemon deve abrir o circuito deste provedor."""

    def __init__(self, retry_after):
        self.retry_after = min(3600, max(1, int(retry_after)))
        super().__init__("quota temporarily unavailable")


class ProviderAuthError(Exception):
    """O provedor recusou a chave informada; `status` é o HTTP da recusa quando houve."""

    def __init__(self, message, status=None):
        """`status`: the HTTP status of the refusal, None when the refusal came inside an answer."""
        super().__init__(message)
        self.status = status


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


def _send(request, timeout, limit_codes=(429,)):
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                exc.close()
                raise ProviderAuthError(f"HTTP {exc.code}", exc.code) from None
            if exc.code not in limit_codes:
                raise
            retry_after = _retry_after_seconds(exc.headers)
            exc.close()
            # One short retry recovers transient limits; a long wait opens the daemon circuit instead.
            if attempt == 0 and exc.code == 429 and (retry_after is None or retry_after <= 2):
                time.sleep(max(0.5, retry_after or 0))
                continue
            raise ProviderRateLimited(retry_after or (300 if exc.code == 429 else MCP_RATE_LIMIT_PAUSE_S)) from None


_CALL = contextvars.ContextVar("web_search_external_call", default=("", False))


def _send_rotating(url, data, method, headers, timeout, limit_codes):
    """Sends the request; a 403 on a keyless call (no credential the vendor could be rejecting) is retried with other
    browser identities, because such a refusal is a block of the client, not of a key."""
    provider, keyless = _CALL.get()
    try:
        return _send(urllib.request.Request(url, data=data, method=method, headers=headers), timeout, limit_codes)
    except ProviderAuthError as exc:
        if not keyless or exc.status != 403:
            raise
        refused = exc
    tried = ["smart-tool"]
    sys.stderr.write(web_identity.format_line(provider, "smart-tool", "HTTP 403", "blocked"))
    for identity in web_identity.attempt_order()[:web_identity.MAX_ATTEMPTS - 1]:
        client_hints = {name: value for name, value in identity.get("headers", {}).items()
                        if name.lower().startswith("sec-ch-")}
        try:
            body = _send(urllib.request.Request(url, data=data, method=method, headers={
                **headers, "User-Agent": identity["user_agent"], **client_hints}), timeout, limit_codes)
        except ProviderAuthError as exc:
            if exc.status != 403:
                raise
            refused = exc
            sys.stderr.write(web_identity.format_line(provider, identity["name"], "HTTP 403", "blocked"))
            tried.append(identity["name"])
            continue
        sys.stderr.write(web_identity.format_line(provider, identity["name"], f"after {', '.join(tried)} blocked",
                                                  "recovered"))
        return body
    sys.stderr.write(web_identity.format_line(provider, tried[-1], f"all {len(tried)} identities blocked", "exhausted"))
    raise refused


def _post(url, body, headers, timeout=7):
    return _send_rotating(url, json.dumps(body).encode("utf-8"), "POST", {
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "User-Agent": "smart-tool", **headers}, timeout, (429,))


def _get(url, headers, timeout=7, limit_codes=(429,)):
    return _send_rotating(url, None, "GET", {"Accept": "application/json", "User-Agent": "smart-tool", **headers},
                          timeout, limit_codes)


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


def _marginalia(query, num, key):
    url = "https://api2.marginalia-search.com/search?" + urllib.parse.urlencode({"query": query, "count": num})
    # 503 is what the shared public key answers when its limit is hit.
    body = _get(url, {"API-Key": key or "public"}, limit_codes=(429, 503))
    try:
        return json.loads(body).get("results") or []
    except (ValueError, AttributeError):
        raise ProviderError("Marginalia returned results in an unexpected format.") from None


def _brave(query, num, key):
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": min(20, num)})
    try:
        return (json.loads(_get(url, {"X-Subscription-Token": key})).get("web") or {}).get("results") or []
    except (ValueError, AttributeError):
        raise ProviderError("Brave returned results in an unexpected format.") from None


def _mojeek(query, num, key):
    url = "https://www.mojeek.com/search?" + urllib.parse.urlencode(
        {"api_key": key, "q": query, "fmt": "json", "t": num})
    try:
        payload = json.loads(_get(url, {})).get("response") or {}
    except (ValueError, AttributeError):
        raise ProviderError("Mojeek returned results in an unexpected format.") from None
    status = str(payload.get("status") or "")
    if status.upper().startswith("ERROR"):
        if "limit" in status.lower():
            raise ProviderRateLimited(3600)
        raise ProviderError(status[:300])
    return [dict(row, content=row.get("desc")) for row in payload.get("results") or [] if isinstance(row, dict)]


def _searxng(query, num, key):
    url = key.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    try:
        body = _get(url, {})
    except ProviderAuthError:
        raise ProviderError("the instance refused the request; enable json in its settings.yml search.formats "
                            "and let this machine through its limiter") from None
    try:
        return json.loads(body).get("results") or []
    except (ValueError, AttributeError):
        raise ProviderError("SearXNG returned results in an unexpected format.") from None


_SEARCH = {"exa": _exa, "parallel": _parallel, "keenable": _keenable, "tavily": _tavily, "firecrawl": _firecrawl,
           "marginalia": _marginalia, "brave": _brave, "mojeek": _mojeek, "searxng": _searxng}


def search(provider, query, num, key=None):
    """Searches one provider and returns up to `num` rows `{title, url, snippet}` with http(s) URLs only.

    `key` defaults to the user's stored key (empty for keyless). Raises ProviderRateLimited on a quota answer,
    ProviderAuthError for a rejected or missing required key, ProviderError for an unreadable answer."""
    if provider not in _SEARCH:
        raise ValueError("Unknown web provider.")
    key = config.get_provider_api_key(provider) if key is None else key
    if provider in config.WEB_PROVIDER_KEY_REQUIRED and not key:
        raise ProviderAuthError(f"{PROVIDERS[provider]['label']} needs your own key or address in the setup screen")
    token = _CALL.set((provider, not key))
    try:
        rows = _SEARCH[provider](query, num, key)
    except ProviderAuthError:
        raise ProviderAuthError("key rejected by the provider") from None
    except (ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        for secret in sorted({key, key.rstrip("/")} - {""}, key=len, reverse=True) if key else ():
            message = message.replace(secret, "***")
        raise ProviderError(message[:300]) from None
    finally:
        _CALL.reset(token)
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
    return len(search(provider, VALIDATION_QUERY, 1, key=key))


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
