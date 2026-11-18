"""Optional external converter for what the native reader of document_read refuses: a file type it does not read and a
PDF with no text layer. Off unless the `document_converter` setting turns it on (see config.document_converter).

The engine (Kreuzberg first, Docling as the drop-in) is never imported here: it runs in a separate interpreter,
isolated with `-I`, under a time cap and a memory cap, and hands its pages back through a file. Whatever it returns
that is empty or thinner than MIN_CHARS_PER_PAGE is an error, never a document."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import atomic_io
import config
import document_text
import process_limits
import version

EXTENSIONS = ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.gif', '.webp', '.doc', '.xls', '.ppt', '.odt', '.ods',
              '.odp', '.rtf')
TIMEOUT_S = 600
MEMORY_BYTES = 4 * 1024 ** 3
MIN_CHARS_PER_PAGE = 40
DETECT_TIMEOUT_S = 30
LOCK_POLL_S = 0.25
STALE_LOCK_S = TIMEOUT_S + 60

KREUZBERG_SCRIPT = r"""
import json, re, sys
import kreuzberg
source, target, force_ocr = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
settings = kreuzberg.ExtractionConfig(output_format="markdown", force_ocr=force_ocr,
                                      pages=kreuzberg.PageConfig(extract_pages=True, insert_page_markers=True))
result = kreuzberg.extract_file_sync(source, config=settings)
split = re.split(r"\\?<\\?!-- PAGE \d+ --\\?>", result.content)
pages = [page.strip() for page in split[1:]] if len(split) > 1 else [result.content]
with open(target, "w", encoding="utf-8") as stream:
    json.dump({"pages": pages}, stream)
"""

DOCLING_SCRIPT = """
import json, sys
from docling.document_converter import DocumentConverter
source, target = sys.argv[1], sys.argv[2]
marker = "<!-- page break -->"
text = DocumentConverter().convert(source).document.export_to_markdown(page_break_placeholder=marker)
with open(target, "w", encoding="utf-8") as stream:
    json.dump({"pages": text.split(marker)}, stream)
"""

ENGINES = {'kreuzberg': ('kreuzberg', KREUZBERG_SCRIPT), 'docling': ('docling', DOCLING_SCRIPT)}
_found = set()
_swept = set()


class Converted:
    """The pages an engine returned for a file: the engine's name and the text of each page."""

    def __init__(self, engine, pages):
        self.engine = engine
        self.pages = pages


def enabled():
    """Whether the `document_converter` setting is anything but off."""
    return config.document_converter()['mode'] != 'off'


def _installed(python, module):
    """Whether `module` can be imported by `python`; a positive answer is kept, a negative one is asked again."""
    if (python, module) in _found:
        return True
    probe = 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)'
    try:
        done = subprocess.run([python, '-I', '-c', probe, module], capture_output=True, timeout=DETECT_TIMEOUT_S,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    if done.returncode == 0:
        _found.add((python, module))
    return done.returncode == 0


def locate():
    """The engine to use and the interpreter that has it.

    Returns: `(name, python)`. Raises: DocumentError naming what was looked for and where when the setting is off, names
    an engine that is not installed, or `auto` finds none."""
    setting = config.document_converter()
    python = setting['python'] or sys.executable
    wanted = list(ENGINES) if setting['mode'] == 'auto' else [setting['mode']]
    if wanted == ['off']:
        raise document_text.DocumentError('The document converter is off.')
    for name in wanted:
        if _installed(python, ENGINES[name][0]):
            return name, python
    raise document_text.DocumentError(
        f"The document converter is set to {setting['mode']} but {' or '.join(wanted)} is not installed in {python}.")


def _checked(engine, pages, expected_pages):
    """The pages if the engine returned enough text, else a DocumentError: under MIN_CHARS_PER_PAGE for each page of the
    document is a failed read, not a document."""
    if not isinstance(pages, list) or not all(isinstance(page, str) for page in pages):
        raise document_text.DocumentError(f'{engine} returned an answer that is not a list of pages.')
    found = sum(len(page.strip()) for page in pages)
    needed = MIN_CHARS_PER_PAGE * max(1, expected_pages or 1)
    if found < needed:
        raise document_text.DocumentError(
            f'{engine} returned {found} characters for {max(1, expected_pages or 1)} page(s), under the {needed} '
            'it takes to count as text: the file was not read.')
    return pages


def cache_folder():
    """The one folder of this version's converted pages: `%LOCALAPPDATA%\\SmartTool\\cache\\<version>\\convert`, created
    here; the folders of other versions are deleted the first time it is asked for in a process.

    Raises: RuntimeError when LOCALAPPDATA is not set."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not set: there is no per-user folder for the converter's cache.")
    root = os.path.join(local, "SmartTool", "cache")
    folder = os.path.join(root, version.VERSION, "convert")
    os.makedirs(folder, exist_ok=True)
    if root not in _swept:
        _swept.add(root)
        for name in os.listdir(root):
            if name != version.VERSION:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    return folder


def _key(path, engine, force_ocr):
    """The name of the result for this content, engine and OCR flag: a SHA-256 over them, never the file's name or place."""
    digest = hashlib.sha256(f"{engine}\0{int(force_ocr)}\0".encode())
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:40]


def _lock(path):
    """Takes the lock file `path` (created exclusively), waiting for another run that holds it; a lock older than
    STALE_LOCK_S belongs to a run that died and is taken over."""
    deadline = time.monotonic() + STALE_LOCK_S
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > STALE_LOCK_S:
                    os.remove(path)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise document_text.DocumentError(f"Another conversion of the same file held {path} for over {STALE_LOCK_S}s.")
            time.sleep(LOCK_POLL_S)


def _cached(result):
    """The pages saved at `result`, or None when there is no such file or it is not a list of pages."""
    try:
        with open(result, encoding="utf-8") as stream:
            pages = json.load(stream)["pages"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pages if isinstance(pages, list) and all(isinstance(page, str) for page in pages) else None


def convert(path, expected_pages=None, force_ocr=False):
    """The text of the file at `path` through the engine the setting picks, in an isolated subprocess.

    Parameters: path (absolute path of the file), expected_pages (page count of the document when known, for the minimum
    of text; None counts one page), force_ocr (read pages as images even when they carry a text layer; used for a PDF
    the native reader found without one). Returns: a Converted. Raises: DocumentError with the reason when no engine is
    installed, the engine fails, is stopped at TIMEOUT_S or MEMORY_BYTES, or returns empty or thin text."""
    engine, python = locate()
    folder = cache_folder()
    stem = os.path.join(folder, _key(path, engine, force_ocr))
    pages = _cached(stem + ".json")
    if pages is None:
        _lock(stem + ".lock")
        try:
            pages = _cached(stem + ".json")
            if pages is None:
                pages = _checked(engine, _engine_pages(engine, python, path, stem + ".work", force_ocr), expected_pages)
                atomic_io.write_text(stem + ".json", json.dumps({"pages": pages}))
        finally:
            os.remove(stem + ".lock")
    return Converted(engine, _checked(engine, pages, expected_pages))


def _engine_pages(engine, python, path, workdir, force_ocr):
    """The pages the engine writes for `path`, run in `workdir` (made and removed here).

    Raises: DocumentError when the engine is stopped, fails or returns no pages."""
    os.makedirs(workdir, exist_ok=True)
    target = os.path.join(workdir, 'pages.json')
    try:
        try:
            code, tail = process_limits.run_capped(
                [python, '-I', '-c', ENGINES[engine][1], path, target, '1' if force_ocr else '0'], workdir, TIMEOUT_S,
                MEMORY_BYTES)
        except subprocess.TimeoutExpired:
            raise document_text.DocumentError(f'{engine} did not finish in {TIMEOUT_S}s and was stopped.') from None
        except OSError as exc:
            raise document_text.DocumentError(f'{engine} could not be started: {exc}') from None
        if code:
            raise document_text.DocumentError(f'{engine} failed (exit {code}): {tail[-300:] or "no error text"}')
        try:
            with open(target, encoding='utf-8') as stream:
                return json.load(stream)['pages']
        except (OSError, ValueError, KeyError, TypeError):
            raise document_text.DocumentError(f'{engine} finished without returning its pages.') from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
