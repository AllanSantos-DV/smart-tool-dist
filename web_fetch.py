"""web_fetch: lê uma URL conhecida e devolve só a resposta ao pedido, feita pelo modelo mini do gateway.

Desenho copiado do WebFetch do Claude Code (Markdown + pedido num modelo separado; code.claude.com/docs/en/tools-reference)
e do OpenCode (`packages/opencode/src/tool/webfetch.ts`: limite de 5 MB, User-Agent de navegador). Medido em 2026-10-05
(docs/spikes/web_fetch_rejudge.py), 37 pares (URL, pedido) reais do WebFetch nativo, juiz cego contra a página
renderizada: melhor resposta 18 × 18 (1 empate), respondeu certo 22 × 16, caracteres devolvidos 35 mil × 72 mil;
3,6 s de mediana, navegador em 7 de 37 (páginas montadas por JavaScript). Repetições da mesma URL nos transcripts:
92 de 124 dentro de 24 h (PAGE_TTL_S).

Páginas acima de PAGE_MAX_CHARS (14,6% do cache, 10,4% das chamadas reais em 2026-10-08) não são mais cortadas no
começo: o texto inteiro fica no cache e o modelo lê os pedaços mais parecidos com o pedido, até SELECT_BUDGET_CHARS, na
ordem da página. Medido em 18 páginas reais e 41 perguntas: trechos depois do corte 0/28 → 13/28, cabeça 6/13 → 5/13,
latência mediana 3,3 s → 3,1 s. Abaixo do limite a página vai inteira (seleção ali empatou em qualidade e piorou a
cauda de latência; docs/BACKLOG.md).
"""
import math
import codecs
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.parse

import trafilatura

import model_client
import research_cache

CACHE_NAMESPACE = "web_fetch"
PAGE_TTL_S = 86400
PAGE_MAX_CHARS = 60_000
PAGE_STORE_MAX_CHARS = 1_000_000
SELECT_BUDGET_CHARS = 15_000
CHUNK_CHARS = 2_000
PAGE_FORMAT = 2
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
HTTP_TIMEOUT_S = 20
READ_CHUNK = 64 * 1024
THIN_CHARS = 1500
PLACEHOLDER_SCAN_CHARS = 5000
MIN_CHARS = 200
ANSWER_MAX_TOKENS = 1500
FAILED_TTL_S = 600
PLACEHOLDERS = ("carregando", "loading...", "enable javascript", "habilite o javascript", "ative o javascript")
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "application/json", "text/csv", "application/xml",
              "text/xml")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0 Safari/537.36")
ANSWER_SYSTEM = (
    "You answer a request about one web page using ONLY the page content provided. Cover every item the request "
    "asks for. Keep exact names, versions, numbers, prices, flags, code and URLs; quote the exact lines when the "
    "request asks for exact values or wording. Say explicitly which requested items the page does not contain. "
    "No preamble. Answer in the language of the request."
)
_failed_lock = threading.Lock()
_failed = {}


class FetchError(RuntimeError):
    pass


def _ip_allowed(ip):
    return ipaddress.ip_address(ip).is_global


def _public_address(host, port):
    """Resolves once; every address must be publicly routable (no private, loopback, link-local, CGNAT/Tailscale)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return None
    addresses = [info[4][0] for info in infos]
    try:
        if not addresses or not all(_ip_allowed(address) for address in addresses):
            return None
    except ValueError:
        return None
    return addresses[0]


def _target(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    address = _public_address(parsed.hostname, port)
    return (parsed, port, address) if address else None


def is_safe_target(url):
    return _target(url) is not None


class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to the address validated by _public_address, so a second DNS answer (rebinding) is never used."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def normalize(url):
    return urllib.parse.urldefrag(str(url or "").strip())[0]


def mark_failed(key):
    with _failed_lock:
        now = time.monotonic()
        for old in [k for k, at in _failed.items() if now - at >= FAILED_TTL_S]:
            del _failed[old]
        _failed[normalize(key)] = now


def recently_failed(key):
    with _failed_lock:
        at = _failed.get(normalize(key))
        return at is not None and time.monotonic() - at < FAILED_TTL_S


def _left(deadline):
    left = deadline - time.monotonic()
    if left <= 1:
        raise TimeoutError("Deadline expired before reading the page.")
    return left


def _get(url, deadline):
    target = _target(url)
    if target is None:
        raise FetchError(f"URL rejected (internal, reserved or invalid network target): {url}")
    parsed, port, address = target
    cls = _PinnedHTTPS if parsed.scheme == "https" else _PinnedHTTP
    conn = cls(parsed.hostname, port, address, min(HTTP_TIMEOUT_S, _left(deadline)))
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Language": "en,pt-BR;q=0.8",
                                           "Accept": "text/markdown, text/html;q=0.9, text/plain;q=0.8, */*;q=0.5"})
        sock = conn.sock
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            return response.status, response.getheader("Location"), None, None
        if response.status >= 400:
            raise FetchError(f"HTTP {response.status} at {url}")
        body = bytearray()
        while True:
            if sock.fileno() != -1:
                sock.settimeout(min(HTTP_TIMEOUT_S, _left(deadline)))
            chunk = response.read(READ_CHUNK)
            if not chunk:
                break
            body += chunk
            if len(body) > MAX_DOWNLOAD_BYTES:
                raise FetchError("Page larger than 5 MB.")
        return response.status, None, response.headers, bytes(body)
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError(f"Network failure at {url}: {exc}") from None
    finally:
        conn.close()


def fetch_http(url, deadline):
    """GET with redirects followed by hand (every hop re-validated). Returns (final_url, content_type, charset, body)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        status, location, headers, body = _get(current, deadline)
        if headers is None:
            if not location:
                raise FetchError(f"HTTP {status} without a target at {current}")
            current = urllib.parse.urljoin(current, location)
            continue
        return current, headers.get_content_type(), headers.get_content_charset(), body
    raise FetchError(f"More than {MAX_REDIRECTS} redirects from {url}.")


def _decode(body, charset):
    try:
        codecs.lookup(charset or "utf-8")
    except LookupError:
        charset = None
    return body.decode(charset or "utf-8", errors="replace")


def thin(text):
    stripped = text.strip()
    if len(stripped) < THIN_CHARS:
        return True
    return len(stripped) < PLACEHOLDER_SCAN_CHARS and any(p in stripped[:600].lower() for p in PLACEHOLDERS)


def read_page(url, deadline, render):
    """Page text (Markdown) from the 24 h cache, HTTP + trafilatura, or `render(url, deadline)` when HTTP comes thin."""
    url = normalize(url)
    for entry in research_cache.candidates(CACHE_NAMESPACE, url, limit=1):
        if (entry.get("query") == url and time.time() - entry["saved_at"] < PAGE_TTL_S
                and entry["result"].get("format") == PAGE_FORMAT):
            return {**entry["result"], "cached": True, "fetched_at": entry["saved_at"]}
    final_url, kind, charset, body = fetch_http(url, deadline)
    rendered, render_error = False, None
    if "html" in kind:
        # Bytes, not str: trafilatura detects the encoding declared in <meta charset> when the header has none.
        text = trafilatura.extract(body, output_format="markdown", include_links=True, include_tables=True,
                                   include_formatting=True, url=final_url) or ""
        if thin(text):
            try:
                markdown = render(final_url, deadline)
            except Exception as exc:
                markdown, render_error = "", f"{type(exc).__name__}: {exc}"[:200]
            if len(markdown.strip()) > len(text.strip()):
                text, rendered = markdown, True
    elif kind in TEXT_TYPES:
        text = _decode(body, charset)
    else:
        raise FetchError(f"Content type {kind} is not read by web_fetch ({final_url}).")
    if len(text.strip()) < MIN_CHARS:
        detail = f"; browser failed: {render_error}" if render_error else ", not even when rendered in the browser"
        raise FetchError(f"Page without readable text ({final_url}){detail}.")
    page = {"url": url, "final_url": final_url, "text": text[:PAGE_STORE_MAX_CHARS], "chars": len(text),
            "truncated": len(text) > PAGE_STORE_MAX_CHARS, "rendered": rendered, "format": PAGE_FORMAT}
    saved_at = research_cache.put(CACHE_NAMESPACE, url, page, {})
    return {**page, "cached": False, "fetched_at": saved_at or time.time(), "render_error": render_error}


def chunks(text, size=CHUNK_CHARS):
    """Consecutive pieces of about size characters, cut at a line break in the second half of each piece."""
    parts, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            end = cut + 1 if cut > start else end
        parts.append(text[start:end])
        start = end
    return parts


def _cosine(a, b):
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def select(text, prompt, embed, budget=SELECT_BUDGET_CHARS):
    """Text the model reads: the whole page up to PAGE_MAX_CHARS; above it, the pieces most similar to the prompt up
    to budget characters, in page order, with [...] where pieces were skipped. embed(texts) returns one vector per
    text and raises when the embedding model is unavailable. Returns (text, number of pieces kept or None)."""
    if len(text) <= PAGE_MAX_CHARS:
        return text, None
    parts = chunks(text)
    vectors = embed(parts + [prompt])
    if len(vectors) != len(parts) + 1:
        raise RuntimeError("The embedding model returned a different number of vectors than page pieces.")
    query = vectors[-1]
    ranked = sorted(range(len(parts)), key=lambda i: -_cosine(vectors[i], query))
    kept, size = [], 0
    for i in ranked:
        if size + len(parts[i]) <= budget:
            kept.append(i)
            size += len(parts[i])
    kept.sort()
    pieces, last = [], None
    for i in kept:
        if last is not None and i != last + 1:
            pieces.append("\n\n[...]\n\n")
        pieces.append(parts[i])
        last = i
    return "".join(pieces), len(kept)


def answer(model, prompt, page, deadline):
    response = model_client.fetch("/v1/chat/completions", model_client.get_token(), method="POST",
                                  timeout=min(90, _left(deadline)), body={
        "model": model, "temperature": 0, "max_tokens": ANSWER_MAX_TOKENS,
        "messages": [{"role": "system", "content": ANSWER_SYSTEM},
                     {"role": "user", "content": json.dumps({"request": prompt, "page": page["text"]},
                                                            ensure_ascii=False)}]})
    text = (response["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("The model returned no answer for the page.")
    return text
