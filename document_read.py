"""read_document: PDF, Word, PowerPoint and Excel files read as compact text, one page, slide or sheet range at a time.

The agent gets the file facts first (type, size, pages, slides or sheets, estimated tokens) and then only the range it
asks for, so a 200-page PDF is never loaded whole. Extraction is the one of document_text (pypdf in an isolated
subprocess for PDF; zipfile and ElementTree with the shared guards for docx, pptx and xlsx); this module adds the unit
split, the markdown shape, the compaction of PDF line wrapping and the range arithmetic."""
import collections
import math
import os
import re
import threading
import unicodedata

import document_text

KINDS = {'.pdf': 'pdf', '.docx': 'docx', '.pptx': 'pptx', '.xlsx': 'xlsx'}
UNIT_NAMES = {'pdf': 'page', 'docx': 'part', 'pptx': 'slide', 'xlsx': 'sheet'}
DEFAULT_MAX_CHARS = 20_000
MAX_CHARS_LIMIT = 100_000
DOCX_PART_CHARS = 6_000
CHARS_PER_TOKEN = 4
CACHE_DOCUMENTS = 4
LONG_LINE = 60

_cache = collections.OrderedDict()
_cache_lock = threading.Lock()


class Unit:
    """One readable unit of a document: a PDF page, a docx part, a slide or a sheet."""

    def __init__(self, number, text, name=''):
        self.number = number
        self.text = text
        self.name = name


class Document:
    """A document split into units, with the facts the metadata block reports."""

    def __init__(self, path, kind, size, units, facts):
        self.path = path
        self.kind = kind
        self.size = size
        self.units = units
        self.facts = facts


def estimate_tokens(text):
    """Token estimate of a text: characters divided by four, rounded up (a rule of thumb, not a tokenizer count).

    Parameters: text. Returns: the estimate as an int."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def compact_pdf_text(text):
    """PDF page text with the layout noise taken out: ligatures and compatibility forms normalised, a hyphen at a line
    end joined to the word, a wrapped line (long, no closing punctuation, next one starting in lower case) joined to the
    next, spaces collapsed and blank-line runs reduced to one.

    Parameters: text (one page as pypdf returns it). Returns: the compact text."""
    text = unicodedata.normalize('NFKC', text).replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'(\w)-\n(?=[a-zà-ÿ])', r'\1', text)
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.split('\n')]
    joined = []
    for line in lines:
        previous = joined[-1] if joined else ''
        if (previous and len(previous) >= LONG_LINE and line[:1].islower()
                and not previous.endswith(('.', ':', ';', '!', '?', ')', '|'))):
            joined[-1] = previous + ' ' + line
        else:
            joined.append(line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(joined)).strip()


def _pdf_units(data):
    """Units and facts of a PDF, one unit per page; raises DocumentError for a password, a corrupt file or a file with no
    text layer at all (scanned pages are not read, there is no OCR)."""
    pages, count = document_text.pdf_pages(data, document_text.PDF_TIMEOUT_S, document_text.PDF_MAX_PAGES,
                                           document_text.PDF_MAX_CHARS)
    if not any(pages):
        raise document_text.DocumentError(
            f'PDF of {count} pages has no text layer (scanned pages are not read; there is no OCR).')
    units = [Unit(number, compact_pdf_text(page) or '(no text on this page)') for number, page in enumerate(pages, 1)]
    facts = {'pages': count, 'pages without text': sum(1 for page in pages if not page)}
    if count > len(pages):
        facts['not read'] = f'pages {len(pages) + 1}-{count} (over the {document_text.PDF_MAX_PAGES} page or ' \
                            f'{document_text.PDF_MAX_CHARS:,} character cap)'
    return units, facts


def _docx_units(data):
    """Units and facts of a docx: the markdown blocks grouped in parts of about DOCX_PART_CHARS characters, a block never
    split (a docx has no fixed pages, they depend on the renderer)."""
    parts, current, size = [], [], 0
    for block in document_text.docx_blocks(data):
        if current and size + len(block) > DOCX_PART_CHARS:
            parts.append('\n'.join(current))
            current, size = [], 0
        current.append(block)
        size += len(block) + 1
    parts.append('\n'.join(current))
    return [Unit(number, text) for number, text in enumerate(parts, 1)], {
        'parts': len(parts), 'note': f'a docx has no fixed pages; a part is about {DOCX_PART_CHARS:,} characters'}


def _pptx_units(data):
    """Units and facts of a pptx: one unit per slide under `[slide N]`, the speaker notes under `[notes N]`."""
    units = []
    for number, body, notes in document_text.pptx_slides(data):
        lines = [f'[slide {number}]'] + (body or ['(no text)']) + ([f'[notes {number}]'] + notes if notes else [])
        units.append(Unit(number, '\n'.join(lines)))
    return units, {'slides': len(units), 'slides without text': sum(1 for u in units if '\n(no text)' in u.text)}


def _xlsx_units(data):
    """Units and facts of an xlsx: one unit per sheet under `[sheet NAME]`, then one tab-separated line per row."""
    units = []
    for number, (name, rows) in enumerate(document_text.xlsx_sheets(data), 1):
        units.append(Unit(number, '\n'.join([f'[sheet {name}]'] + (rows or ['(no text)'])), name))
    return units, {'sheet names': ', '.join(f'{u.number}={u.name}' for u in units)}


_BUILDERS = {'pdf': _pdf_units, 'docx': _docx_units, 'pptx': _pptx_units, 'xlsx': _xlsx_units}


def load(path):
    """The document at `path`, split into units. Parsed documents are kept for a few calls (keyed by path, size and
    modification time), so reading page after page parses the file once.

    Parameters: path (absolute path of a .pdf, .docx, .pptx or .xlsx file). Returns: a Document. Raises: ValueError for a
    relative path, a missing file, an unsupported type or a file over the size limit of its type; DocumentError with a
    readable reason for a file the extractor refuses (corrupt, encrypted, hostile, scanned, without text)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("read_document requires 'path'")
    if not os.path.isabs(path):
        raise ValueError(f'read_document needs an absolute path, got {path!r}')
    extension = os.path.splitext(path)[1].lower()
    if extension not in KINDS:
        raise ValueError(f"read_document reads {', '.join(KINDS)} files; {extension or 'this file'} is not supported "
                         '(use the normal Read for plain text; legacy .doc, .xls and .ppt and macro files are not read)')
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ValueError(f'cannot read {path}: {exc.strerror or type(exc).__name__}') from None
    if not os.path.isfile(path):
        raise ValueError(f'{path} is not a file')
    if info.st_size > document_text.size_limit(path):
        raise ValueError(f'{path} is {info.st_size / 1048576:.1f} MiB, over the '
                         f'{document_text.size_limit(path) // 1048576} MiB limit of a {extension} file')
    key = (os.path.normcase(path), info.st_size, info.st_mtime_ns)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    with open(path, 'rb') as stream:
        data = stream.read()
    kind = KINDS[extension]
    units, facts = _BUILDERS[kind](data)
    document = Document(path, kind, info.st_size, units, facts)
    with _cache_lock:
        _cache[key] = document
        while len(_cache) > CACHE_DOCUMENTS:
            _cache.popitem(last=False)
    return document


def parse_range(spec, document):
    """Unit numbers selected by a range text: `3`, `3-5`, `7-` (to the end), `-4` (the first four), a comma list of
    those (`1,4-6`) and, for a workbook, a sheet name (case-insensitive).

    Parameters: spec (the text), document (a Document). Returns: the sorted list of unit numbers. Raises: ValueError
    naming the valid range for a malformed or out-of-range spec or an unknown sheet name."""
    count = len(document.units)
    unit = UNIT_NAMES[document.kind]
    chosen = set()
    for part in (piece.strip() for piece in spec.split(',')):
        if document.kind == 'xlsx':
            named = [u.number for u in document.units if u.name.lower() == part.lower()]
            if named:
                chosen.update(named)
                continue
        found = re.fullmatch(r'(\d*)\s*(-?)\s*(\d*)', part)
        if not part or not found or not (found.group(1) or found.group(3)):
            raise ValueError(f'range {spec!r} is not valid: use a {unit} number, a span like 3-5, 7- or -4, or a comma list'
                             + ('; a sheet name also works' if document.kind == 'xlsx' else ''))
        first, dash, last = found.groups()
        if not dash and last:
            raise ValueError(f'range {spec!r} is not valid: use a {unit} number, a span like 3-5, 7- or -4, or a comma list')
        low = int(first) if first else 1
        high = int(last) if last else (count if dash else low)
        if low < 1 or high > count or low > high:
            raise ValueError(f'range {spec!r} is outside the document: it has {count} {unit}s (1-{count})')
        chosen.update(range(low, high + 1))
    return sorted(chosen)


def _spans(numbers):
    """Compact text of unit numbers: `1-3, 7`."""
    spans, start, previous = [], None, None
    for number in numbers + [None]:
        if start is None:
            start = previous = number
        elif number is not None and number == previous + 1:
            previous = number
        else:
            spans.append(str(start) if start == previous else f'{start}-{previous}')
            start = previous = number
    return ', '.join(spans)


def header(document):
    """The metadata block that opens every answer: path, type, size, units and the estimated tokens of the whole text.

    Parameters: document. Returns: the block as text."""
    total = sum(estimate_tokens(unit.text) for unit in document.units)
    lines = [f'document: {document.path}',
             f'type: {document.kind} | size: {document.size / 1048576:.2f} MiB | '
             f'{UNIT_NAMES[document.kind]}s: {len(document.units)} | est. tokens (all text): {total:,}']
    lines += [f'{key}: {value}' for key, value in document.facts.items()
              if value and key != UNIT_NAMES[document.kind] + 's']
    return '\n'.join(lines)


def read(path, range_spec=None, max_chars=DEFAULT_MAX_CHARS, info_only=False):
    """The answer of read_document: the metadata block, then the chosen units as markdown, a note naming the next range
    when the text was cut.

    Parameters: path (see load), range_spec (see parse_range; None reads from the first unit up to max_chars),
    max_chars (cap of the text returned, 1000-MAX_CHARS_LIMIT; a single unit over the cap is cut at a line with a
    notice), info_only (only the metadata block). Returns: the text. Raises: as load and parse_range, ValueError for a
    max_chars out of bounds."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1000 <= max_chars <= MAX_CHARS_LIMIT:
        raise ValueError(f'max_chars must be an integer from 1000 to {MAX_CHARS_LIMIT}')
    document = load(path)
    head = header(document)
    if info_only:
        return head
    numbers = parse_range(range_spec, document) if range_spec else list(range(1, len(document.units) + 1))
    unit = UNIT_NAMES[document.kind]
    body, shown, size, cut = [], [], 0, None
    for number in numbers:
        text = document.units[number - 1].text
        label = f'[{unit} {number}]\n' if document.kind in ('pdf', 'docx') else ''
        piece = label + text
        if shown and size + len(piece) > max_chars:
            break
        if len(piece) > max_chars:
            lines = piece.split('\n')
            kept, used = [], 0
            for line in lines:
                if used + len(line) + 1 > max_chars and kept:
                    break
                kept.append(line)
                used += len(line) + 1
            piece = '\n'.join(kept)
            cut = (f'{unit} {number} cut after {len(kept)} of {len(lines)} lines; raise max_chars '
                   f'(up to {MAX_CHARS_LIMIT:,}) to read the rest')
        body.append(piece)
        shown.append(number)
        size += len(piece) + 1
        if cut:
            break
    returned = '\n\n'.join(body)
    notes = [f'returned: {unit}s {_spans(shown)} (est. {estimate_tokens(returned):,} tokens)']
    remaining = [number for number in numbers if number > shown[-1]]
    if cut:
        notes.append(cut)
    if remaining:
        notes.append(f'next: range="{remaining[0]}-{remaining[-1]}"' if not range_spec
                     else f'not returned: {unit}s {_spans(remaining)} (max_chars); ask for them with range')
    return '\n'.join([head] + notes + ['---', returned])
