#!/usr/bin/env python3
"""Navegador Camoufox residente: uma instância, abas reaproveitadas, protocolo JSON por linha.

Entrada (stdin): {"id": str, "engine": "google|duckduckgo|startpage", "query": str, "num": int, "expires_at": epoch}
Saída (stdout): {"ready": true} ao subir; depois {"id": str, "results": [...]} ou
{"id": str, "error": str, "captcha": bool, "skipped": bool}.
Encerra sozinho após IDLE_S sem pedidos, quando o stdin fecha ou quando o navegador cai.
"""
import asyncio
import json
import os
import sys
import time
import urllib.parse
import uuid

TABS = 2
IDLE_S = 300
PAGE_OP_TIMEOUT_S = 10
# Google flagged the IP after ~150 searches in bursts of 4; 40 sequential searches ~4 s apart passed.
MIN_GAP_S = {"google": 4.0}
# Startpage's Anubis challenge can hold a tab for minutes; never let it take every tab.
MAX_CONCURRENT = {"startpage": 1}
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions", "browser.storage_state.json")

GOOGLE_JS = (
    "() => [...document.querySelectorAll('#search a:has(h3)')].map(a => { "
    "const box = a.closest('[data-hveid], .g') || a.parentElement; "
    "const s = box && box.querySelector('.VwiC3b, [data-sncf], [style*=\"line-clamp\"]'); "
    "return {url: a.href, title: a.querySelector('h3').innerText, snippet: s ? s.innerText : ''}; })"
)
DDG_JS = (
    "() => [...document.querySelectorAll('.result')].map(el => { "
    "const a = el.querySelector('.result__a'); const s = el.querySelector('.result__snippet'); "
    "return {url: a ? (a.getAttribute('href') || '') : '', title: a ? (a.textContent || '').trim() : '', "
    "snippet: s ? (s.textContent || '').trim() : ''}; }).filter(r => r.url)"
)
STARTPAGE_JS = (
    "() => [...document.querySelectorAll('.result')].map(el => { "
    "const t = el.querySelector('[class*=\"result-title\" i]'); const a = t || el.querySelector('a'); "
    "const s = el.querySelector('p, [class*=\"description\" i], [class*=\"snippet\" i]'); "
    "return {url: a ? a.href : '', title: a ? (a.textContent || '').trim() : '', "
    "snippet: s ? (s.textContent || '').trim() : ''}; }).filter(r => r.url)"
)
STARTPAGE_CHALLENGE_MARKERS = ("anubis", "verifying your request", "making sure you're not a bot")


class Captcha(Exception):
    pass


class Skipped(Exception):
    pass


def _ddg_real_url(href):
    parsed = urllib.parse.urlparse(href if href.startswith("http") else f"https:{href}")
    target = urllib.parse.parse_qs(parsed.query).get("uddg")
    return target[0] if target else href


async def _body_text(page):
    return (await page.evaluate("document.body ? document.body.innerText : ''"))[:3000].lower()


async def google(page, query, timeout_s):
    await page.goto("https://www.google.com/search?" + urllib.parse.urlencode({"hl": "en", "q": query}),
                    wait_until="domcontentloaded", timeout=timeout_s * 1000)
    if "/sorry/" in page.url:
        raise Captcha("google: /sorry/ page (unusual traffic)")
    try:
        await page.wait_for_selector("#search a:has(h3)", timeout=min(4, timeout_s) * 1000)
    except Exception:
        if "unusual traffic" in await _body_text(page):
            raise Captcha("google: unusual traffic")
    rows = await page.evaluate(GOOGLE_JS)
    targets = await asyncio.gather(*(_google_target(page, row["url"]) for row in rows))
    return [{**row, "url": target} for row, target in zip(rows, targets) if target]


async def _google_target(page, href):
    # Result links are opaque /goto?url=... redirects; the session resolves them without following.
    parsed = urllib.parse.urlparse(href)
    if not parsed.netloc.endswith("google.com") or not parsed.path.startswith(("/goto", "/url")):
        return href
    try:
        response = await page.context.request.get(href, max_redirects=0, timeout=5000)
    except Exception:
        return None
    return response.headers.get("location")


async def duckduckgo(page, query, timeout_s):
    await page.goto("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}),
                    wait_until="domcontentloaded", timeout=timeout_s * 1000)
    try:
        await page.wait_for_selector(".result__a", timeout=min(8, timeout_s) * 1000)
    except Exception:
        if "bots use duckduckgo too" in await _body_text(page):
            raise Captcha("duckduckgo: image challenge")
    return [{**row, "url": _ddg_real_url(row["url"])} for row in await page.evaluate(DDG_JS)]


async def startpage(page, query, timeout_s):
    started = time.monotonic()
    await page.goto("https://www.startpage.com/sp/search?" + urllib.parse.urlencode({"query": query}),
                    wait_until="domcontentloaded", timeout=timeout_s * 1000)
    while True:
        rows = await page.evaluate(STARTPAGE_JS)
        if rows:
            return rows
        text = await _body_text(page)
        challenged = any(marker in text for marker in STARTPAGE_CHALLENGE_MARKERS)
        if not challenged and time.monotonic() - started > 5:
            return []
        if time.monotonic() - started > timeout_s - 2:
            return []
        await asyncio.sleep(1)


ENGINES = {"google": google, "duckduckgo": duckduckgo, "startpage": startpage}


class Service:
    def __init__(self, context, emit, exit_process):
        self.context = context
        self.emit = emit
        self.exit_process = exit_process
        self.pages = asyncio.Queue()
        self.page_lock = asyncio.Lock()
        self.pace_locks = {engine: asyncio.Lock() for engine in MIN_GAP_S}
        self.last_run = {}
        self.limits = {engine: asyncio.Semaphore(n) for engine, n in MAX_CONCURRENT.items()}
        self.last_activity = time.monotonic()
        self.in_flight = 0

    async def start(self):
        for _ in range(TABS):
            self.pages.put_nowait(await self._new_page())

    async def _new_page(self):
        # Opening several tabs at once hangs Camoufox navigations; tabs are created one at a time.
        async with self.page_lock:
            page = await asyncio.wait_for(self.context.new_page(), PAGE_OP_TIMEOUT_S)
            await asyncio.wait_for(page.goto("about:blank"), PAGE_OP_TIMEOUT_S)
            return page

    async def _recycle(self, page, healthy):
        try:
            if not healthy:
                raise asyncio.TimeoutError
            await asyncio.wait_for(page.goto("about:blank"), PAGE_OP_TIMEOUT_S)
            return page
        except Exception:
            try:
                await asyncio.wait_for(page.close(), PAGE_OP_TIMEOUT_S)
            except Exception:
                pass
        try:
            return await self._new_page()
        except Exception as exc:
            print(f"tab could not be recreated; browser shut down: {exc}", file=sys.stderr, flush=True)
            self.exit_process(1)

    async def save_session(self):
        try:
            os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
            tmp = f"{SESSION_PATH}.{uuid.uuid4().hex}.tmp"
            state = await asyncio.wait_for(self.context.storage_state(), PAGE_OP_TIMEOUT_S)
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(state, stream)
            os.replace(tmp, SESSION_PATH)
        except Exception as exc:
            print(f"session not saved: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    def _left(self, request):
        return float(request["expires_at"]) - time.time()

    async def _pace(self, engine, request):
        gap = MIN_GAP_S.get(engine)
        if not gap:
            return
        async with self.pace_locks[engine]:
            wait = self.last_run.get(engine, 0) + gap - time.monotonic()
            if wait > 0:
                if wait >= self._left(request) - 1:
                    raise Skipped(f"{engine}: minimum interval between searches exceeds the deadline")
                await asyncio.sleep(wait)
            self.last_run[engine] = time.monotonic()

    async def handle(self, request):
        self.in_flight += 1
        try:
            self.emit(await self._run(request))
        finally:
            self.in_flight -= 1
            self.last_activity = time.monotonic()

    async def _run(self, request):
        engine_name = request.get("engine")
        if engine_name not in ENGINES:
            return {"id": request.get("id"), "error": f"unknown engine: {engine_name!r}", "captcha": False}
        limit = self.limits.get(engine_name)
        try:
            if limit:
                await asyncio.wait_for(limit.acquire(), max(0.01, self._left(request)))
            try:
                return await self._run_on_tab(request, engine_name)
            finally:
                if limit:
                    limit.release()
        except asyncio.TimeoutError:
            return self._skipped(request, f"{engine_name}: another search holds the engine past the deadline")
        except Skipped as exc:
            return self._skipped(request, str(exc))

    def _skipped(self, request, reason):
        return {"id": request.get("id"), "error": reason, "captcha": False, "skipped": True}

    async def _run_on_tab(self, request, engine_name):
        try:
            page = await asyncio.wait_for(self.pages.get(), max(0.01, self._left(request)))
        except asyncio.TimeoutError:
            raise Skipped(f"{engine_name}: no free tab before the deadline")
        healthy = True
        try:
            await self._pace(engine_name, request)
            left = self._left(request)
            if left < 1:
                raise Skipped(f"{engine_name}: deadline expired before running")
            rows = await asyncio.wait_for(ENGINES[engine_name](page, request["query"], left), left)
            rows = [r for r in rows if str(r.get("url", "")).startswith("http")][:int(request.get("num") or 8)]
            if rows and engine_name == "startpage":
                await self.save_session()
            return {"id": request["id"], "results": rows}
        except Skipped:
            raise
        except Captcha as exc:
            return {"id": request["id"], "error": str(exc), "captcha": True}
        except Exception as exc:
            healthy = not isinstance(exc, asyncio.TimeoutError)
            return {"id": request["id"], "error": f"{engine_name}: {type(exc).__name__}: {exc}"[:400],
                    "captcha": False}
        finally:
            self.pages.put_nowait(await self._recycle(page, healthy))


def emit(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def main():
    from camoufox.async_api import AsyncCamoufox

    loop = asyncio.get_running_loop()
    browser = await AsyncCamoufox(headless=True).__aenter__()
    browser.on("disconnected", lambda *_: os._exit(1))
    state = None
    try:
        with open(SESSION_PATH, encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError):
        pass
    service = Service(await browser.new_context(storage_state=state), emit, os._exit)
    await service.start()
    emit({"ready": True})
    tasks = set()
    reader = loop.run_in_executor(None, sys.stdin.readline)
    while True:
        done, _ = await asyncio.wait({reader}, timeout=5)
        if not done:
            if service.in_flight == 0 and time.monotonic() - service.last_activity > IDLE_S:
                break
            continue
        line = reader.result()
        if not line:
            break
        reader = loop.run_in_executor(None, sys.stdin.readline)
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
        except ValueError:
            print(f"invalid line ignored: {line[:200]!r}", file=sys.stderr, flush=True)
            continue
        service.last_activity = time.monotonic()
        task = asyncio.create_task(service.handle(request))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.wait(tasks, timeout=10)
    try:
        await asyncio.wait_for(browser.close(), PAGE_OP_TIMEOUT_S)
    finally:
        os._exit(0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    asyncio.run(main())
