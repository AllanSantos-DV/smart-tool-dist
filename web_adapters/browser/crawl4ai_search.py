#!/usr/bin/env python3
# Uso: python crawl4ai_search.py "<url semente>" [--max-pages 5]
# Saida: JSON no stdout -- {"pages": [{"url": ..., "markdown": ...}, ...]}
# Tier 4 do web_search do Smart Tool: crawl multi-pagina (BFS, mesmo dominio) a partir
# de uma URL ja resolvida pelo chamador (nao faz busca — quem decide a URL semente e o
# daemon, via Tier 1, antes de invocar este script).
import asyncio
import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

from crawl4ai import AsyncWebCrawler, BFSDeepCrawlStrategy, CacheMode, CrawlerRunConfig
from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLFilter
from playwright.async_api import Error as PlaywrightError

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

MAX_REDIRECT_HOPS = 5


def _is_safe_target(url):
    # Mesma checagem do daemon (_is_safe_crawl_target): resolve o host de
    # verdade via DNS, não confia só na string da URL, recusa IP privado/
    # loopback/link-local/reservado/multicast. Duplicada aqui (não importada
    # do daemon) porque este script é quem de fato faz a navegação de rede —
    # a validação do daemon sozinha não cobre redirect HTTP nem link
    # descoberto durante o crawl, só a URL semente original.
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addrs = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for _family, _type, _proto, _canonname, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # deixa o 3xx virar HTTPError em vez de seguir automaticamente


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoAutoRedirect)


def resolve_safe_final_url(url, max_hops=MAX_REDIRECT_HOPS):
    # Segue a cadeia de redirect manualmente, validando CADA salto antes de
    # seguir — sem isto, uma URL semente pública (que passa a validação
    # inicial) responder com "302 Location: http://127.0.0.1:.../status"
    # levaria o browser real a seguir o redirect sem qualquer checagem, já
    # que o navegador do Crawl4AI segue 3xx de forma transparente.
    current = url
    for _ in range(max_hops + 1):
        if not _is_safe_target(current):
            raise ValueError(f"URL rejected (internal/reserved network target): {current}")
        req = urllib.request.Request(current, headers={"User-Agent": "Mozilla/5.0"})
        try:
            resp = _NO_REDIRECT_OPENER.open(req, timeout=10)
            resp.close()
            return current
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                return current  # erro não-redirect (404 etc.) — deixa o crawler real reportar
            location = e.headers.get("Location")
            if not location:
                raise ValueError("Redirect without a Location header")
            current = urllib.parse.urljoin(current, location)
    raise ValueError("Too many redirects while resolving the seed URL")


class _SSRFSafeURLFilter(URLFilter):
    # Primeira triagem, barata, de cada link descoberto no BFS antes de
    # sequer abrir uma página pra ele — a defesa real (que cobre redirect
    # HTTP em qualquer salto e navegação por meta-refresh/JS, que este
    # filtro de string não alcança) é o route guard via Playwright abaixo.
    def apply(self, url):
        try:
            return _is_safe_target(url)
        except Exception:
            return False


_MAX_ROUTE_REDIRECT_HOPS = 5


async def _abort_route(route):
    try:
        await route.abort()
    except PlaywrightError:
        # O contexto pode fechar enquanto um sub-recurso ainda está em rota.
        # Nesse estado a requisição já foi descartada pelo navegador.
        pass


async def _ssrf_route_guard(route):
    # Cobre a navegação (inicial, redirect HTTP em qualquer salto, e
    # meta-refresh/JS) e sub-recursos da PÁGINA em que foi instalado — o que
    # `page.route()` alcança. Novas páginas/abas (popup, target="_blank") e
    # WebSocket ficam fora do alcance dessa API e são bloqueados por outros
    # mecanismos (ver `_install_ssrf_route_guard`), não por este guard.
    # Playwright não emite um novo evento de rota pro destino de um redirect
    # HTTP — o browser segue o 3xx internamente — por isso a cadeia é seguida
    # manualmente via route.fetch(max_redirects=0), validando cada salto
    # antes de buscar o próximo; route.fulfill só ocorre quando o destino
    # final também é seguro, e qualquer salto reprovado aborta a requisição.
    current_url = route.request.url
    for _ in range(_MAX_ROUTE_REDIRECT_HOPS + 1):
        try:
            # to_thread pra não bloquear o event loop enquanto outras páginas
            # do mesmo lote (arun_many) carregam em paralelo.
            safe = await asyncio.to_thread(_is_safe_target, current_url)
        except Exception:
            safe = False
        if not safe:
            await _abort_route(route)
            return
        try:
            response = await route.fetch(url=current_url, max_redirects=0)
        except Exception:
            await _abort_route(route)
            return
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                await _abort_route(route)
                return
            current_url = urllib.parse.urljoin(current_url, location)
            continue
        try:
            await route.fulfill(response=response)
        except PlaywrightError:
            await _abort_route(route)
        return
    await _abort_route(route)


async def _ssrf_ws_guard(ws_route):
    # page.route() não intercepta WebSocket (API separada do Playwright) —
    # sem isto, JS na página estabeleceria a conexão TCP direto, sem passar
    # por _is_safe_target nenhuma vez. Extração de markdown não depende de
    # WebSocket, então a conexão é sempre fechada sem nunca chamar
    # connect_to_server (o servidor real nunca é contatado).
    await ws_route.close()


async def _install_ssrf_route_guard(page, context=None, config=None):
    # Registrar no context (não no page) cobre também popup/nova aba
    # (window.open, target="_blank") desde a primeira requisição — routing
    # por Page só existe depois que a página já foi criada, tarde o
    # bastante pra uma navegação inicial disparada por popup escapar sem
    # checagem.
    target = context if context is not None else page
    if getattr(target, "_ssrf_guard_installed", False):
        return
    target._ssrf_guard_installed = True
    # As duas chamadas abaixo não são atômicas — depende deste hook disparar
    # estritamente antes de qualquer goto()/JS no context (garantia do
    # crawl4ai enquanto cada página nasce em about:blank sem reaproveitar
    # sessão/context já em uso; deixaria de valer se o script um dia passar
    # a configurar reuso de sessão/context no BrowserConfig).
    await asyncio.gather(
        target.route("**/*", _ssrf_route_guard),
        target.route_web_socket("**/*", _ssrf_ws_guard),
    )


def parse_args(argv):
    max_pages = 5
    url_parts = []
    i = 0
    while i < len(argv):
        if argv[i] == "--max-pages" and i + 1 < len(argv):
            max_pages = int(argv[i + 1])
            i += 2
        else:
            url_parts.append(argv[i])
            i += 1
    if not url_parts:
        raise ValueError('Usage: python crawl4ai_search.py "<seed url>" [--max-pages 5]')
    return " ".join(url_parts), max_pages


async def crawl(seed_url, max_pages):
    safe_seed_url = resolve_safe_final_url(seed_url)
    config = CrawlerRunConfig(
        deep_crawl_strategy=BFSDeepCrawlStrategy(
            max_depth=2, max_pages=max_pages, filter_chain=FilterChain([_SSRFSafeURLFilter()]),
        ),
        cache_mode=CacheMode.BYPASS,
        page_timeout=30000,
    )
    strategy = AsyncPlaywrightCrawlerStrategy()
    strategy.set_hook("on_page_context_created", _install_ssrf_route_guard)
    async with AsyncWebCrawler(crawler_strategy=strategy) as crawler:
        results = await crawler.arun(url=safe_seed_url, config=config)
        return [
            {"url": r.url, "markdown": r.markdown.raw_markdown}
            for r in results
            if r.success and r.markdown and r.markdown.raw_markdown
        ]


def main():
    seed_url, max_pages = parse_args(sys.argv[1:])
    pages = asyncio.run(crawl(seed_url, max_pages))
    if not pages:
        print("No page extracted: the target may have blocked the crawler or has no content.", file=sys.stderr)
        sys.exit(1)
    sys.stdout.buffer.write(json.dumps({"pages": pages}, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
