"""web_fetch_many: several URLs with one prompt in one call, each read through the single-URL path of web_fetch.

The caller passes that path as `fetch_one(url, prompt)`: it returns the text of one URL or raises. Every URL is read on
its own (cache, failure memory, browser fallback, address guard and answer of web_fetch), so one URL that fails is
reported with its reason next to the others instead of failing or answering for them. A small pool keeps the number of
pages read at once, and so of browsers started, low.
"""
import concurrent.futures
import contextvars

import web_fetch

MAX_URLS = 8
WORKERS = 4
MAX_URL_CHARS = 2000
MAX_PROMPT_CHARS = 4000


def check(urls, prompt):
    """Raises ValueError unless `urls` is a list of 1 to MAX_URLS non-empty strings and `prompt` a non-empty string, so
    nothing is read for a malformed call."""
    if not isinstance(urls, list) or not urls:
        raise ValueError(f"web_fetch_many requires 'urls' (a list of 1 to {MAX_URLS} URLs)")
    if len(urls) > MAX_URLS:
        raise ValueError(f"web_fetch_many reads at most {MAX_URLS} URLs per call, got {len(urls)}: split them in calls "
                         f"of {MAX_URLS} or fewer")
    if not all(isinstance(url, str) and url.strip() and len(url) <= MAX_URL_CHARS for url in urls):
        raise ValueError(f"web_fetch_many requires every URL to be a non-empty string of at most {MAX_URL_CHARS} "
                         "characters")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"web_fetch_many requires 'prompt' (at most {MAX_PROMPT_CHARS} characters)")


def read_many(urls, prompt, fetch_one):
    """One result per entry of `urls`, in the same order: `{"url", "ok": True, "text"}` or `{"url", "ok": False,
    "error"}`. A URL repeated (same address once the fragment and the outer spaces are dropped) is read once and
    every position gets its result. Each read runs in a copy of the caller's context, so the request attribution and
    the model-failure collection of the tool call follow it into the pool."""
    keys = [web_fetch.normalize(url) for url in urls]
    outcomes = read_pool({key: key for key in keys}, lambda key: fetch_one(key, prompt))
    return [{"url": url, **outcomes[key]} for url, key in zip(urls, keys)]


def read_pool(jobs, read_one):
    """Runs `read_one(argument)` for every entry of `jobs` ({key: argument}) in a pool of at most WORKERS threads, each in
    a copy of the caller's context. Returns {key: {"ok": True, "text"} or {"ok": False, "error"}}: a read that raises is
    reported under its key and never stops the others."""
    with concurrent.futures.ThreadPoolExecutor(min(WORKERS, len(jobs)), thread_name_prefix="read-many") as pool:
        futures = {key: pool.submit(contextvars.copy_context().run, read_one, argument) for key, argument in jobs.items()}
    return {key: _outcome(future) for key, future in futures.items()}


def _outcome(future):
    try:
        return {"ok": True, "text": future.result()}
    except Exception as exc:
        return {"ok": False, "error": str(exc) or type(exc).__name__}


def render_batch(tool, noun, done, results, label):
    """The text the agent reads for a batch tool: a count line, then one numbered block per item with its text or
    `ERROR: reason`.

    Parameters: tool (name in the count line), noun (what the items are, plural), done (past participle of a success),
    results ([{"ok", "text" or "error", ...}] in order), label (key of each result that names its block)."""
    failed = sum(not result["ok"] for result in results)
    blocks = [f"{tool}: {len(results)} {noun}, {len(results) - failed} {done}, {failed} failed."]
    for number, result in enumerate(results, 1):
        body = result["text"] if result["ok"] else f"ERROR: {result['error']}"
        blocks.append(f"[{number}/{len(results)}] {result[label]}\n{str(body).rstrip()}")
    return "\n\n".join(blocks)


def render(results):
    """The text the agent reads: a count line, then one numbered block per URL with its answer or `ERROR: reason`."""
    return render_batch("web_fetch_many", "URLs", "read", results, "url")
