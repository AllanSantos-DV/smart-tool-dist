"""read_document_many: several documents (local paths or http(s) URLs) with the same range and limits in one call, each
read through the single-document path of read_document.

The caller passes that path as `read_one(path)`: it returns the text of one document or raises. Every document is read
on its own (size limits, URL download and 24 h kept copy, range check, answer of read_document), so one document that
fails is reported with its reason next to the others. The pool is the one of web_fetch_many.
"""
import os

import document_read
import document_url
import web_fetch
import web_fetch_many

MAX_DOCUMENTS = 8
MAX_PATH_CHARS = 2000
SHARED_FIELDS = ("range", "max_chars", "info_only")


def check(paths, shared):
    """Raises ValueError unless `paths` is a list of 1 to MAX_DOCUMENTS non-empty strings and the shared arguments are
    well formed (`range` text, `max_chars` in bounds, `info_only` true or false), so nothing is read for a malformed
    call and the same mistake is not reported once per document."""
    if not isinstance(paths, list) or not paths:
        raise ValueError(f"read_document_many requires 'paths' (a list of 1 to {MAX_DOCUMENTS} paths or URLs)")
    if len(paths) > MAX_DOCUMENTS:
        raise ValueError(f"read_document_many reads at most {MAX_DOCUMENTS} documents per call, got {len(paths)}: split "
                         f"them in calls of {MAX_DOCUMENTS} or fewer")
    if not all(isinstance(path, str) and path.strip() and len(path) <= MAX_PATH_CHARS for path in paths):
        raise ValueError(f"read_document_many requires every path to be a non-empty string of at most {MAX_PATH_CHARS} "
                         "characters")
    if "range" in shared and not isinstance(shared["range"], str):
        raise ValueError("read_document_many requires 'range' to be text such as `3-5`")
    if "max_chars" in shared:
        document_read.check_max_chars(shared["max_chars"])
    if "info_only" in shared and not isinstance(shared["info_only"], bool):
        raise ValueError("read_document_many requires 'info_only' to be true or false")


def _key(path):
    """What makes two entries the same document: a URL without fragment and outer spaces, a path as the file system
    compares it."""
    text = path.strip()
    return web_fetch.normalize(text) if document_url.is_url(text) else os.path.normcase(os.path.normpath(text))


def read_many(paths, read_one):
    """One result per entry of `paths`, in the same order: `{"path", "ok": True, "text"}` or `{"path", "ok": False,
    "error"}`. An entry repeated (same key, see `_key`) is read once, as first written, and every position gets its
    result."""
    keys = [_key(path) for path in paths]
    first = {}
    for key, path in zip(keys, paths):
        first.setdefault(key, path.strip())
    outcomes = web_fetch_many.read_pool(first, read_one)
    return [{"path": _label(path), **outcomes[key]} for path, key in zip(paths, keys)]


def _label(path):
    """The entry as a block names it: a URL without credentials, query or fragment (as read_document names it), a path as
    given."""
    if not document_url.is_url(path.strip()):
        return path
    try:
        return document_url.shown(path.strip())
    except ValueError:
        return path


def distinct(paths):
    """How many documents `read_many` reads for `paths`."""
    return len({_key(path) for path in paths})


def render(results):
    """The text the agent reads: a count line, then one numbered block per document with its text or `ERROR: reason`."""
    failed = sum(not result["ok"] for result in results)
    blocks = [f"read_document_many: {len(results)} documents, {len(results) - failed} read, {failed} failed."]
    for number, result in enumerate(results, 1):
        body = result["text"] if result["ok"] else f"ERROR: {result['error']}"
        blocks.append(f"[{number}/{len(results)}] {result['path']}\n{body}")
    return "\n\n".join(blocks)
