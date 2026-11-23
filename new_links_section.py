"""The `new_links` section of a change review, run only when asked: the http(s) links the changed HTML and Markdown files add
(`link_check.external` over the text before and after) are read through `web_fetch_many` with the probe of `ext_links`, the
guarded download of `web_fetch`, and the dead or undecided ones are listed. At most MAX_URLS links are read and the rest is
counted. It is advisory: nothing here blocks, an `error` is not proof the page is gone, and a check that cannot run says why.
"""
import collections

import ext_links
import link_check
import web_fetch
import web_fetch_many

MAX_URLS = 10
STATES = ("dead", "error")


def _section(enabled, checked, findings=(), problem="", found=0, read=0):
    return {"enabled": enabled, "checked": checked, "findings": list(findings), "problem": problem, "found": found,
            "read": read, "not_checked": found - read}


def disabled():
    """The section of a review that was not asked to read the links: nothing was read and nothing is said."""
    return _section(False, False)


def describe(finding):
    """One sentence for a finding of `review`: the link, what became of the read and where it is written."""
    places = ", ".join(f"{place['path']}:{place['line']}" for place in finding["places"])
    state = f"is dead (HTTP {finding['status']})" if finding["state"] == "dead" else f"could not be read ({finding['detail']})"
    return f"`{finding['url']}` {state}, written at {places}"


def cut_sentence(left):
    """The sentence that says `left` new links were not read because of MAX_URLS."""
    return f"{left} more new link(s) were not read: the check reads the first {MAX_URLS}."


def _added(versions):
    """The links the diff adds: ({normalized url: [{"path", "line"}]} in order of appearance, [reason a file's links were not
    read whole]). A link whose text was already in the file before is not the diff's."""
    places, unread = {}, []
    for rel, full, before, after in versions:
        if after is None:
            continue
        found = link_check.external(full, after)
        old = link_check.external(full, before) if before else {"links": [], "diagnostics": []}
        known = collections.Counter(web_fetch.normalize(link["url"]) for link in old["links"])
        unread += [f"{rel}: {reason}" for reason in found["diagnostics"] if reason not in old["diagnostics"]]
        for link in found["links"]:
            url = web_fetch.normalize(link["url"])
            if known[url] > 0:
                known[url] -= 1
            else:
                places.setdefault(url, []).append({"path": rel, "line": link["line"]})
    return places, unread


def review(versions, fetch_one=None):
    """The new external links of the changed files that look dead.

    Parameters: versions (iterable) of (project-relative path, absolute path, text before or None for a new file, text after
    or None for a deleted one); fetch_one (callable or None) `fetch_one(url, prompt)` reading one URL and raising when it
    cannot, `ext_links.probe` when None.
    Returns: dict {"enabled": True, "checked": bool, "findings": [{"url", "state" (dead or error), "status" (int or None),
    "detail", "places": [{"path", "line"}]}] dead first, "problem": str (why it was not checked whole, "" when it was),
    "found": int (distinct new links), "read": int, "not_checked": int (links past MAX_URLS)}.
    """
    try:
        places, unread = _added(list(versions))
    except (OSError, ValueError, RuntimeError) as exc:
        return _section(True, False, problem=f"the new links could not be read: {exc}")
    urls = list(places)[:MAX_URLS]
    problem = "; ".join(unread)
    if not urls:
        return _section(True, not problem, problem=problem)
    probe = fetch_one or ext_links.probe
    statuses = {}

    def read(url, prompt):
        try:
            return probe(url, prompt)
        except Exception as exc:
            statuses[url] = getattr(exc, "status", None)
            raise

    outcomes = {result["url"]: result for result in web_fetch_many.read_many(urls, ext_links.PROMPT, read)}
    findings = []
    for url in urls:
        state, status, detail = ext_links._verdict(url, outcomes[url], statuses.get(url))
        if state in STATES:
            findings.append({"url": url, "state": state, "status": status, "detail": detail, "places": places[url]})
    findings.sort(key=lambda item: STATES.index(item["state"]))
    return _section(True, not problem, findings, problem, found=len(places), read=len(urls))
