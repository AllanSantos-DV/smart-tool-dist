"""read_document by URL: the file is downloaded with the web_fetch download (public addresses only, every redirect hop
checked, 5 MB cap) into the per-user cache folder and then read by document_read like a local file, so ranges and the
token estimate work the same. A web page is refused with the pointer to web_fetch. The query string and the
credentials of a URL are never part of the answer or of a file name."""
import hashlib
import os
import time
import urllib.parse

import atomic_io
import document_converter
import document_read
import web_fetch

FOLDER = "download"
URL_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".epub")
CONTENT_TYPES = {"application/pdf": ".pdf", "text/csv": ".csv", "application/epub+zip": ".epub",
                 "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
                 **{kind: "." + extension for kind, extension in web_fetch.OFFICE_TYPES.items()}}
EXTENSION_FALLBACK_TYPES = web_fetch.GENERIC_BINARY_TYPES + web_fetch.TEXT_TYPES


def is_url(value):
    """Whether `value` is read as a URL: a string that starts with `http://` or `https://`, nothing else."""
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def shown(url):
    """The URL as an answer names it: scheme, host, port and path, without credentials, query or fragment.

    Raises: ValueError when the URL cannot be parsed."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("read_document could not parse the URL") from None
    if not host:
        raise ValueError("read_document needs a host in the URL")
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path}"


def extension(url):
    """Lowercase extension of the URL path (`.pdf`), empty when there is none or the URL cannot be parsed; this is what
    the metric records, so the query string never reaches it."""
    try:
        return os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    except ValueError:
        return ""


def _document_extension(kind, final_url):
    """File extension for the downloaded bytes: by content type, or by the extension of the final URL path when the
    server sends a generic or text type. Raises ValueError, naming web_fetch, for a web page, and naming the readable
    types for anything else."""
    name = shown(final_url)
    if "html" in kind:
        raise ValueError(f"{name} is a web page ({kind}), not a document: use web_fetch with this URL and a prompt "
                         "to read it.")
    found = CONTENT_TYPES.get(kind)
    if not found and kind in EXTENSION_FALLBACK_TYPES and extension(final_url) in URL_EXTENSIONS:
        found = extension(final_url)
    if not found:
        raise ValueError(f"{name} is {kind}, which is not a document read_document reads "
                         f"({', '.join(URL_EXTENSIONS)}); for a web page use web_fetch.")
    return found


def _stale_sweep(folder):
    """Deletes the downloads older than the 24 h web_fetch keeps a page."""
    cutoff = time.time() - web_fetch.PAGE_TTL_S
    for entry in os.scandir(folder):
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            try:
                os.remove(entry.path)
            except OSError:
                continue


def download(url, deadline):
    """The local path of the file at `url`: the copy downloaded in the last 24 h, else a fresh download kept in
    `<cache>/download` under a name made of a SHA-256 of the URL (never the URL, the query or the file name).

    Parameters: url (http or https), deadline (time.monotonic() value). Returns: the path. Raises: FetchError for a
    refused address, a failed or oversized download; ValueError for a web page or a type that is not a document;
    TimeoutError past the deadline."""
    folder = document_converter.cache_folder(FOLDER)
    _stale_sweep(folder)
    stem = hashlib.sha256(web_fetch.normalize(url).encode()).hexdigest()[:40]
    for name in os.listdir(folder):
        if name.startswith(stem + "."):
            return os.path.join(folder, name)
    final_url, kind, _charset, body = web_fetch.fetch_http(web_fetch.normalize(url), deadline)
    path = os.path.join(folder, stem + _document_extension(kind, final_url))
    atomic_io.write_secret_bytes(path, body)
    return path


def read(url, range_spec, max_chars, info_only, deadline):
    """The answer of read_document for an http(s) URL: the file downloaded (or kept from a recent read) and read by
    document_read, with the URL, without its query and credentials, on the first line.

    Parameters: as document_read.read, with the URL for the path and the deadline of the download. Returns: the text.
    Raises: as download and document_read.read."""
    label = shown(url)
    document_read.check_max_chars(max_chars)
    return document_read.read(download(url, deadline), range_spec, max_chars, info_only, label)
