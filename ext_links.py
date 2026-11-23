"""Link rot check of the external links in the project docs, run only when asked: the http(s) links of the README and `docs`
files are collected with `link_check` and each distinct one is read through `web_fetch_many`, over the guarded download of
`web_fetch` (redirects followed, internal addresses refused). Every checked URL gets its own result, `ok`, `dead` or `error`,
with the HTTP status when the server answered one, and the number of URLs read is capped. Nothing is written.
"""
import time
from collections import defaultdict

import link_check
import project_scan
import rename_preview
import web_fetch
import web_fetch_many

DEFAULT_LIMIT = 30
MAX_URLS = 100
PROBE_TIMEOUT_S = 30
PLACES = 5
DEAD_STATUSES = (404, 410)
PROMPT = "link rot check"
NOTE = ("Read from the git-tracked README and `docs` text (the generated docs/scope is left out): the http(s) links of "
        "Markdown and HTML, not bare URLs in the text. `dead` is an HTTP 404 or 410; `error` is a link that could not be "
        "decided (another status, a blocked or slow server, a network failure, an address the guarded download refuses) "
        "and is not proof the page is gone. An `ok` page was read; its status is not kept.")


def probe(url, _prompt):
    """Reads one URL through the guarded download of `web_fetch`, in the `fetch_one(url, prompt)` form `web_fetch_many`
    takes, and returns the address the read ended at.

    Raises: web_fetch.FetchError (with the HTTP status when there was one), TimeoutError.
    """
    return web_fetch.fetch_http(url, time.monotonic() + PROBE_TIMEOUT_S)[0]


def _collect(root):
    """The distinct external links of the docs, in the order they first appear, with the places that write each.

    Returns: tuple (dict {url: [{"path", "line"}]}, docs checked, docs unreadable, list of {"path", "reason"}).
    Raises: ValueError when git cannot list the folder.
    """
    places, checked, unreadable, unread = defaultdict(list), 0, 0, []
    for rel, text in project_scan.tracked_texts(root, link_check.EXTENSIONS):
        if not rename_preview._is_doc(rel):
            continue
        if text is None:
            unreadable += 1
            continue
        checked += 1
        found = link_check.external(rel, text)
        unread += [{"path": rel, "reason": reason} for reason in found["diagnostics"]]
        for link in found["links"]:
            places[web_fetch.normalize(link["url"])].append({"path": rel, "line": link["line"]})
    return places, checked, unreadable, unread


def _verdict(url, outcome, status):
    """The state, status and detail of one read of `url`, given its `web_fetch_many` outcome and the HTTP status the failed
    read carried (None when it carried none)."""
    if outcome["ok"]:
        moved = web_fetch.normalize(outcome["text"]) != url
        return "ok", None, f"redirected to {outcome['text']}" if moved else "reachable"
    return ("dead" if status in DEAD_STATUSES else "error"), status, outcome["error"]


def check(root, limit=DEFAULT_LIMIT, fetch_one=probe):
    """Reads the distinct external links of the project docs and says which are gone.

    Parameters: root (str) project folder, a git repository; limit (int) distinct URLs read, 1 to MAX_URLS; fetch_one
    (callable) `fetch_one(url, prompt)` reading one URL, returning its text or address and raising when it cannot.
    Returns: dict {"urls": [{"url", "state": "ok", "dead" or "error", "status": int or None, "detail", "places": [{"path",
    "line"}] up to PLACES, "places_total"}] with the dead first, then the errors, "counts": {"docs_checked",
    "docs_unreadable", "found", "checked", "not_checked", "ok", "dead", "error"}, "unread": [{"path", "reason"}] (links the
    reader could not take whole), "summary": str, "note": str}.
    Raises: ValueError when limit is not an integer from 1 to MAX_URLS or git cannot list the folder.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_URLS:
        raise ValueError(f"ext_links reads at most {MAX_URLS} URLs per call: limit must be an integer from 1 to {MAX_URLS}, "
                         f"got {limit!r}.")
    places, docs, unreadable, unread = _collect(root)
    urls = list(places)[:limit]
    statuses = {}

    def read(url, prompt):
        try:
            return fetch_one(url, prompt)
        except Exception as exc:
            statuses[url] = getattr(exc, "status", None)
            raise

    outcomes = {result["url"]: result for result in web_fetch_many.read_many(urls, PROMPT, read)} if urls else {}
    rows = []
    for url in urls:
        state, status, detail = _verdict(url, outcomes[url], statuses.get(url))
        rows.append({"url": url, "state": state, "status": status, "detail": detail, "places": places[url][:PLACES],
                     "places_total": len(places[url])})
    rows.sort(key=lambda row: ("dead", "error", "ok").index(row["state"]))
    by_state = {state: sum(row["state"] == state for row in rows) for state in ("ok", "dead", "error")}
    counts = {"docs_checked": docs, "docs_unreadable": unreadable, "found": len(places), "checked": len(rows),
              "not_checked": len(places) - len(rows), **by_state}
    summary = (f"{len(rows)} external link(s) read from {docs} doc file(s): {by_state['ok']} ok, {by_state['dead']} dead, "
               f"{by_state['error']} error" if rows else f"No external link in {docs} doc file(s)")
    if counts["not_checked"]:
        summary += f"; {counts['not_checked']} more were not read, raise limit (at most {MAX_URLS})"
    return {"urls": rows, "counts": counts, "unread": unread, "summary": summary + ".", "note": NOTE}
