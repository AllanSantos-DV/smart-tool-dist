"""Local links an edit of an HTML or Markdown file adds or changes, checked against the files on disk, for the edit hook.
Links come from `web_document_graph.analyze` (the same reader the code graph uses). A link counts only when its text is
new in the file (a link that was already broken is not the edit's), and only relative paths are looked up: http(s),
mailto, tel, data and #anchors are skipped. What cannot be decided is returned as unchecked with the reason instead of
being dropped: a root-relative path (the web root is not known), a template expression, a page with a <base> element,
a file that cannot be read, HTML that did not parse."""
import collections
import os
import re

import web_document_graph

EXTENSIONS = (".html", ".htm", ".md", ".markdown")
MAX_URL_CHARS = 300  # web_document_graph keeps the first 300 characters of a link
_TEMPLATE = re.compile(r"\{\{|\{%|<%|<\?|\$\{")
_BASE = re.compile(r"<base[\s>]", re.I)


def _links(origin, text):
    """The local links of text (their specifiers and lines) and the parser diagnostics, in document order."""
    found = web_document_graph.analyze({origin: text}, ())
    return [item for item in found["dependencies"] if item["resolution"] == "unresolved"], found["diagnostics"]


def _missing(target):
    """Whether target does not exist; any other failure to look it up raises OSError."""
    try:
        os.stat(target)
    except (FileNotFoundError, NotADirectoryError):
        return True
    return False


def external(path, text):
    """The http(s) links of an HTML or Markdown file, as `web_document_graph.analyze` reads them.

    Parameters: path (str) path of the file, which only picks the reader; text (str) its contents.
    Returns: dict {"links": [{"line", "url"}] in document order, "diagnostics": [reason]} (HTML that did not parse); both
    empty for a file that is not HTML or Markdown.
    """
    if os.path.splitext(path)[1].lower() not in EXTENSIONS:
        return {"links": [], "diagnostics": []}
    found = web_document_graph.analyze({os.path.abspath(path).replace(os.sep, "/"): text}, ())
    web = [item for item in found["dependencies"]
           if item["resolution"] == "external" and item["specifier"].lower().startswith(("http://", "https://"))]
    cut = [item for item in web if len(item["specifier"]) >= MAX_URL_CHARS]
    return {"links": [{"line": item["line"], "url": item["specifier"]} for item in web if item not in cut],
            "diagnostics": [item["reason"] for item in found["diagnostics"]]
                           + [f"line {item['line']}: a URL of {MAX_URL_CHARS} characters or more was not read whole"
                              for item in cut]}


def review(path, before, after):
    """The local links the edit introduced that point to nothing, and the ones that could not be checked.

    Parameters: path (str) absolute path of the edited file; before (str or None) text before the edit; after (str) text
    after it.
    Returns: dict with `broken` (list of {line, link}) and `unchecked` (list of {line, link, why}; line and link are None
    for a parse failure of the whole file); both empty for a file that is not HTML or Markdown.
    """
    result = {"broken": [], "unchecked": []}
    if os.path.splitext(path)[1].lower() not in EXTENSIONS:
        return result
    origin = os.path.abspath(path).replace(os.sep, "/")
    known = collections.Counter(item["specifier"] for item in _links(origin, before)[0]) if before else collections.Counter()
    links, diagnostics = _links(origin, after)
    result["unchecked"] += [{"line": None, "link": None, "why": item["reason"]} for item in diagnostics]
    has_base = bool(_BASE.search(after)) and origin.lower().endswith((".html", ".htm"))
    for item in links:
        link = item["specifier"]
        if known[link] > 0:
            known[link] -= 1
            continue
        why = None
        if has_base:
            why = "the page has a <base> element, which changes where relative links lead"
        elif _TEMPLATE.search(link):
            why = "it is a template expression, not a path"
        elif link.startswith("/"):
            why = "it is relative to the root of a site or repository, which is not known here"
        else:
            try:
                if _missing(web_document_graph.local_target(origin, link)):
                    result["broken"].append({"line": item["line"], "link": link})
            except (OSError, ValueError) as exc:
                why = f"it could not be looked up: {exc}"
        if why:
            result["unchecked"].append({"line": item["line"], "link": link, "why": why})
    return result
