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
import datetime
import difflib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.parse

import config
import document_text
import model_client
import research_cache
import version
import web_identity

CACHE_NAMESPACE = "web_fetch"
PAGE_TTL_S = 86400
PAGE_MAX_CHARS = 60_000
PAGE_STORE_MAX_CHARS = 1_000_000
SELECT_BUDGET_CHARS = 15_000
CHUNK_CHARS = 2_000
DIFF_MAX_LINES = 20
DIFF_LINE_CHARS = 160
PAGE_FORMAT = 2
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
PDF_INTERACTIVE_TIMEOUT_S = 30  # Override of document_text.PDF_TIMEOUT_S: a person waits for this answer, the indexer does not.
MAX_REDIRECTS = 5
HTTP_TIMEOUT_S = 20
READ_CHUNK = 64 * 1024
THIN_CHARS = 1500
PLACEHOLDER_SCAN_CHARS = 5000
MIN_CHARS = 200
ANSWER_MAX_TOKENS = 1500
FAILED_TTL_S = 600
PLACEHOLDERS = ("carregando", "loading...", "enable javascript", "habilite o javascript", "ative o javascript")
OFFICE_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx"}
GENERIC_BINARY_TYPES = ("application/octet-stream", "application/zip", "binary/octet-stream")
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "application/json", "text/csv", "application/xml",
              "text/xml")
DEFAULT_IDENTITY = "chrome-win"
BLOCK_RETRY_DELAY_S = 0.4
REFUSAL_STATUSES = (403, 429)
PROJECT_URL = "https://github.com/AllanSantos-DV/smart-tool"
# Wikimedia's User-Agent policy (foundation.wikimedia.org, Policy:Wikimedia_Foundation_User-Agent_Policy) asks bots for a
# descriptive agent with contact information and treats a browser's agent used by a bot as malicious.
WIKIMEDIA_DOMAINS = ("wikipedia.org", "wikimedia.org", "mediawiki.org", "wikidata.org", "wiktionary.org",
                     "wikibooks.org", "wikiquote.org", "wikisource.org", "wikinews.org", "wikiversity.org",
                     "wikivoyage.org", "wikifunctions.org")
ANSWER_SYSTEM = (
    "You answer a request about one web page using ONLY the page content provided. Cover every item the request "
    "asks for. Keep exact names, versions, numbers, prices, flags, code and URLs; quote the exact lines when the "
    "request asks for exact values or wording. Say explicitly which requested items the page does not contain. "
    "No preamble. Answer in the language of the request."
)
_failed_lock = threading.Lock()
_failed = {}


class FetchError(RuntimeError):
    """A page that could not be read; `status` is the HTTP status when the server answered one, otherwise None."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _ip_allowed(ip):
    return ipaddress.ip_address(ip).is_global


def _public_address(host, port):
    """Resolves once; every address must be publicly routable (no private, loopback, link-local, CGNAT)."""
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


def is_wikimedia(host):
    """Whether `host` is a Wikimedia project domain or one of its subdomains."""
    host = (host or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in WIKIMEDIA_DOMAINS)


def _wikimedia_user_agent():
    return version.user_agent(config.load_config().get("contact") or PROJECT_URL)


def _get(url, deadline, identity=None):
    """One GET. `identity` (a web_identity entry) sets the browser headers; Wikimedia hosts always get the
    descriptive agent instead."""
    target = _target(url)
    if target is None:
        raise FetchError(f"URL rejected (internal, reserved or invalid network target): {url}")
    parsed, port, address = target
    cls = _PinnedHTTPS if parsed.scheme == "https" else _PinnedHTTP
    conn = cls(parsed.hostname, port, address, min(HTTP_TIMEOUT_S, _left(deadline)))
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        wanted = {"Accept-Language": "en,pt-BR;q=0.8",
                  "Accept": "text/markdown, text/html;q=0.9, text/plain;q=0.8, */*;q=0.5"}
        headers = ({"User-Agent": _wikimedia_user_agent(), **wanted} if is_wikimedia(parsed.hostname)
                   else web_identity.request_headers(identity or web_identity.load_identities()[0], **wanted))
        conn.request("GET", path, headers=headers)
        sock = conn.sock
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            return response.status, response.getheader("Location"), None, None
        if response.status >= 400:
            raise FetchError(f"HTTP {response.status} at {url}", response.status)
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


def _get_rotating(url, deadline):
    """`_get` that answers a 403 or 429 by retrying with other coherent browser identities; the refusal is raised once
    the identities are used up. Wikimedia hosts are never rotated: their policy wants one honest, descriptive agent."""
    host = urllib.parse.urlsplit(url).hostname or ""
    provider = f"web_fetch/{host}"
    if is_wikimedia(host):
        try:
            return _get(url, deadline)
        except FetchError as exc:
            if exc.status in REFUSAL_STATUSES:
                web_identity.record([web_identity.format_line(provider, "descriptive", f"HTTP {exc.status}", "blocked")])
            raise
    tried = []
    for identity in web_identity.attempt_order(first=DEFAULT_IDENTITY):
        if tried:
            time.sleep(min(BLOCK_RETRY_DELAY_S, max(0, _left(deadline) - 1)))
        try:
            result = _get(url, deadline, identity)
        except FetchError as exc:
            if exc.status not in REFUSAL_STATUSES:
                raise
            refused = exc
            tried.append(identity["name"])
            web_identity.record([web_identity.format_line(provider, identity["name"], f"HTTP {exc.status}", "blocked")])
            continue
        if tried:
            web_identity.record([web_identity.format_line(provider, identity["name"],
                                                          f"after {', '.join(tried)} blocked", "recovered")])
        return result
    web_identity.record([web_identity.format_line(provider, tried[-1], f"all {len(tried)} identities blocked",
                                                  "exhausted")])
    raise refused


def fetch_http(url, deadline):
    """GET with redirects followed by hand (every hop re-validated). Returns (final_url, content_type, charset, body)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        status, location, headers, body = _get_rotating(current, deadline)
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


def pdf_text(body, url, deadline):
    """Text of a PDF (pypdf, pure Python), page by page, with the page and character caps of document_text. The native
    WebFetch reads PDFs and the hook sends it here, so a PDF used to be a dead end (9 times in 10 days of transcripts).
    A hostile PDF can make pypdf parse for minutes, so it runs in a subprocess killed at the request's deadline (at most
    PDF_INTERACTIVE_TIMEOUT_S, passed to document_text.run_pdf instead of the indexer's document_text.PDF_TIMEOUT_S)."""
    timeout = min(PDF_INTERACTIVE_TIMEOUT_S, _left(deadline))
    try:
        return document_text.run_pdf(body, timeout, document_text.PDF_MAX_PAGES, document_text.PDF_MAX_CHARS)
    except document_text.PdfTimeout:
        raise FetchError(f"PDF not read in {timeout:.0f}s ({url}).") from None
    except document_text.PdfUnreadable as exc:
        raise FetchError(f"PDF could not be read ({url}): {exc.detail}"[:300]) from None
    except document_text.PdfEncrypted:
        raise FetchError(f"PDF protected by a password is not read by web_fetch ({url}).") from None


def office_extension(kind, url):
    """`docx`, `pptx` or `xlsx` when the response is an Office file: by its content type, or by the extension in the URL
    path when the server sends a generic binary type; None otherwise."""
    if kind in OFFICE_TYPES:
        return OFFICE_TYPES[kind]
    extension = urllib.parse.urlsplit(url).path.rsplit(".", 1)[-1].lower()
    return extension if kind in GENERIC_BINARY_TYPES and extension in OFFICE_TYPES.values() else None


def office_text(body, extension, url):
    """Text of a docx, pptx or xlsx with the markers and caps of document_text (the extractor the index uses, with its
    archive guards); FetchError with the reason when the file cannot be read."""
    try:
        return document_text.extract(f"file.{extension}", body).decode("utf-8", errors="replace")
    except document_text.DocumentError as exc:
        raise FetchError(f"{extension} file could not be read ({url}): {exc}"[:300]) from None


def _extract_markdown(body, url):
    """Main text of an HTML page as Markdown ('' when the extractor finds none). `body` is bytes, not str: trafilatura
    detects the encoding declared in <meta charset> when the header has none."""
    import trafilatura  # imported here: its 250 modules and 23 MB are only needed by the first HTML page read
    return trafilatura.extract(body, output_format="markdown", include_links=True, include_tables=True,
                               include_formatting=True, url=url) or ""


def compare(old, new, read_at):
    """How a page differs from its previous read, by line (difflib.unified_diff, no context lines).

    Parameters: old and new (the two page texts), read_at (epoch seconds of the previous read). Returns: a dict with
    `read_at`, `changed`, the `added` and `removed` line counts and `diff`, every changed line in page order with its
    `+` or `-` (the file and hunk headers left out).

    Technical doc: docs/scope/technical/_root.md#web_fetch.py::compare"""
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))[2:]
    diff = [line for line in lines if not line.startswith("@@")]
    return {"read_at": read_at, "changed": bool(diff), "added": sum(line.startswith("+") for line in diff),
            "removed": sum(line.startswith("-") for line in diff), "diff": diff}


def change_note(since):
    """What the answer says about a page already read before: the clause for its Source line and the short diff under it.

    Parameters: since (the dict of compare). Returns: (clause such as `; changed since the previous read at
    2026-11-26 14:03 (+2 -1 lines)` or `; unchanged since ...`, the lines of the diff block: a title and at most
    DIFF_MAX_LINES changed lines of at most DIFF_LINE_CHARS characters; none when the page is unchanged).

    Technical doc: docs/scope/technical/_root.md#web_fetch.py::change_note"""
    stamp = datetime.datetime.fromtimestamp(since["read_at"]).strftime("%Y-%m-%d %H:%M")
    if not since["changed"]:
        return f"; unchanged since the previous read at {stamp}", []
    total = len(since["diff"])
    size = f"{total} lines" if total <= DIFF_MAX_LINES else f"first {DIFF_MAX_LINES} of {total} lines"
    return (f"; changed since the previous read at {stamp} (+{since['added']} -{since['removed']} lines)",
            [f"Changes since the previous read (unified diff, {size}):"]
            + [line[:DIFF_LINE_CHARS] for line in since["diff"][:DIFF_MAX_LINES]])


def read_page(url, deadline, render):
    """Page text (Markdown) from the 24 h cache, HTTP + trafilatura, or `render(url, deadline)` when HTTP comes thin.
    A page downloaded again while an older copy of the same URL is still in the cache carries `since`, the compare of
    the two texts.

    Technical doc: docs/scope/technical/_root.md#web_fetch.py::read_page"""
    url = normalize(url)
    previous = None
    for entry in research_cache.candidates(CACHE_NAMESPACE, url, limit=1):
        if entry.get("query") == url and entry["result"].get("format") == PAGE_FORMAT:
            if time.time() - entry["saved_at"] < PAGE_TTL_S:
                return {**entry["result"], "cached": True, "fetched_at": entry["saved_at"]}
            previous = entry
    final_url, kind, charset, body = fetch_http(url, deadline)
    rendered, render_error = False, None
    if "html" in kind:
        text = _extract_markdown(body, final_url)
        if thin(text):
            try:
                markdown = render(final_url, deadline)
            except Exception as exc:
                markdown, render_error = "", f"{type(exc).__name__}: {exc}"[:200]
            if len(markdown.strip()) > len(text.strip()):
                text, rendered = markdown, True
    elif kind in TEXT_TYPES:
        text = _decode(body, charset)
    elif kind == "application/pdf":
        text = pdf_text(body, final_url, deadline)
    elif office_extension(kind, final_url):
        text = office_text(body, office_extension(kind, final_url), final_url)
    else:
        raise FetchError(f"Content type {kind} is not read by web_fetch ({final_url}).")
    if len(text.strip()) < MIN_CHARS:
        detail = f"; browser failed: {render_error}" if render_error else ", not even when rendered in the browser"
        raise FetchError(f"Page without readable text ({final_url}){detail}.")
    page = {"url": url, "final_url": final_url, "text": text[:PAGE_STORE_MAX_CHARS], "chars": len(text),
            "truncated": len(text) > PAGE_STORE_MAX_CHARS, "rendered": rendered, "format": PAGE_FORMAT}
    saved_at = research_cache.put(CACHE_NAMESPACE, url, page, {})
    fetched = {**page, "cached": False, "fetched_at": saved_at or time.time(), "render_error": render_error}
    if previous:
        fetched["since"] = compare(previous["result"].get("text") or "", page["text"], previous["saved_at"])
    return fetched


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
