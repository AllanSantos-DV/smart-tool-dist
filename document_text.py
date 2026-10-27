"""Extração local de texto DOCX, PPTX, XLSX e PDF, sem Office, execução de macros ou acesso externo."""
import contextlib
import io
import json
import posixpath
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
import zlib

VERSION = 3
PDF_MAX_BYTES = 20 * 1024 * 1024
PDF_TIMEOUT_S = 20
PDF_MAX_PAGES = 300
PDF_MAX_CHARS = 1_000_000
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
XLSX_MAX_ROWS = 100_000
XLSX_MAX_COLUMNS = 16_384
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


class DocumentError(ValueError):
    pass


class PdfTimeout(DocumentError):
    pass


class PdfEncrypted(DocumentError):
    pass


class PdfUnreadable(DocumentError):
    def __init__(self, message, detail):
        super().__init__(message)
        self.detail = detail


class _DocumentTreeBuilder(ET.TreeBuilder):
    label = 'DOCX'

    def doctype(self, name, pubid, system):
        # O callback também recusa DTD em XML UTF-16, antes de expandir entidades.
        raise DocumentError(f'{self.label} contains disallowed entities.')


class _Package:
    """An open OOXML zip: the parts it holds and a guarded parser that charges every part to one XML budget."""

    def __init__(self, archive, entries, label):
        self.archive = archive
        self.entries = entries
        self.label = label
        self.xml_bytes = 0

    def parse(self, name):
        info = self.entries[name]
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise DocumentError(f'Encrypted {self.label} or unsupported compression.')
        if info.file_size > MAX_XML_BYTES - self.xml_bytes:
            raise DocumentError(f'{self.label} XML content over the limit.')
        with self.archive.open(info) as stream:
            raw = stream.read(MAX_XML_BYTES - self.xml_bytes + 1)
        self.xml_bytes += len(raw)
        if self.xml_bytes > MAX_XML_BYTES or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise DocumentError(f'{self.label} contains excessive XML or disallowed entities.')
        builder = _DocumentTreeBuilder()
        builder.label = self.label
        return ET.fromstring(raw, parser=ET.XMLParser(target=builder))


@contextlib.contextmanager
def _open_package(data, label, required):
    """The one place that opens an OOXML zip (docx, pptx, xlsx) with the guards: archive size, entry count, a required
    part, no encryption, one XML budget, no DOCTYPE or ENTITY, and every low-level failure turned into a DocumentError.

    Parameters: data (zip bytes), label (`DOCX`, `PPTX` or `XLSX`, used in the reasons), required (a part name, or a
    predicate over the part names, that must exist). Yields: a _Package. Raises: DocumentError with a readable reason."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise DocumentError(f'{label} over the 10 MiB limit.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            listing = archive.infolist()
            entries = {info.filename: info for info in listing}
            present = required(entries) if callable(required) else required in entries
            if len(listing) > 2000 or not present:
                raise DocumentError(f'Invalid {label} or excessive structure.')
            yield _Package(archive, entries, label)
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError, OSError, zlib.error, EOFError) as exc:
        raise DocumentError(f'Could not extract text from this {label}.') from exc


class _Lines(list):
    """Output lines of an extractor; `add` refuses to grow past MAX_TEXT_BYTES of UTF-8 text."""

    def __init__(self, label):
        super().__init__()
        self.label = label
        self.size = 0

    def add(self, *lines):
        for line in lines:
            self.size += len(line.encode('utf-8')) + 1
            if self.size > MAX_TEXT_BYTES:
                raise DocumentError(f'Text extracted from {self.label} exceeds 1 MiB.')
            self.append(line)


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def extract_docx(data):
    """Text of a .docx: body, headers, footers, footnotes and endnotes, one line per non-empty paragraph.

    Parameters: data (the file bytes). Returns: UTF-8 text bytes. Raises: DocumentError with a readable reason for a
    corrupt, encrypted or oversized package, or one with a DOCTYPE or entity."""
    with _open_package(data, 'DOCX', 'word/document.xml') as package:
        names = ['word/document.xml'] + sorted(name for name in package.entries if re.fullmatch(
            r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml', name))
        paragraphs = _Lines('DOCX')
        for name in names:
            root = package.parse(name)
            namespace = W if root.tag.startswith(W) else '{http://purl.oclc.org/ooxml/wordprocessingml/main}'
            for paragraph in root.iter(namespace + 'p'):
                def parts(node):
                    pending = list(reversed(node))
                    while pending:
                        child = pending.pop()
                        if child.tag == namespace + 'p':
                            continue
                        if child.tag == namespace + 't':
                            yield child.text or ''
                        elif child.tag == namespace + 'tab':
                            yield '\t'
                        elif child.tag in (namespace + 'br', namespace + 'cr'):
                            yield '\n'
                        else:
                            pending.extend(reversed(child))
                text = ''.join(parts(paragraph))
                if text.strip():
                    paragraphs.add(text)
        return ('\n'.join(paragraphs) + '\n' if paragraphs else '').encode('utf-8')


def _drawing_paragraphs(root):
    """Non-empty paragraphs (`a:p`) of a slide or notes part, from the `a:t` runs; the slide-number field is not text."""
    lines = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != 'p':
            continue
        pending = list(reversed(paragraph))
        pieces = []
        while pending:
            child = pending.pop()
            kind = _local(child.tag)
            if kind == 'fld' and child.get('type', '').startswith('slidenum'):
                continue
            if kind == 't':
                pieces.append(child.text or '')
            elif kind == 'br':
                pieces.append('\n')
            elif kind != 'p':
                pending.extend(reversed(child))
        text = ''.join(pieces)
        if text.strip():
            lines.append(text)
    return lines


def extract_pptx(data):
    """Text of a .pptx: each slide under `[slide N]`, then its speaker notes under `[notes N]`, in slide order.

    Parameters: data (the file bytes). Returns: UTF-8 text bytes. Raises: DocumentError with a readable reason for a
    corrupt, encrypted, oversized or hostile package, or a deck without any text."""
    def has_slides(names):
        return any(re.fullmatch(r'ppt/slides/slide\d+\.xml', name) for name in names)

    with _open_package(data, 'PPTX', has_slides) as package:
        slides = sorted((int(m.group(1)), name) for name in package.entries
                        if (m := re.fullmatch(r'ppt/slides/slide(\d+)\.xml', name)))
        lines = _Lines('PPTX')
        for number, name in slides:
            body = _drawing_paragraphs(package.parse(name))
            if body:
                lines.add(f'[slide {number}]', *body)
            notes_name = f'ppt/notesSlides/notesSlide{number}.xml'
            notes = _drawing_paragraphs(package.parse(notes_name)) if notes_name in package.entries else []
            if notes:
                lines.add(f'[notes {number}]', *notes)
    if not lines:
        raise DocumentError('PPTX has no text.')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def _shared_strings(package):
    if 'xl/sharedStrings.xml' not in package.entries:
        return []
    strings = []
    for item in package.parse('xl/sharedStrings.xml'):
        if _local(item.tag) != 'si':
            continue
        pending = list(reversed(item))
        pieces = []
        while pending:
            child = pending.pop()
            kind = _local(child.tag)
            if kind == 't':
                pieces.append(child.text or '')
            elif kind != 'rPh':
                pending.extend(reversed(child))
        strings.append(''.join(pieces))
    return strings


def _column_index(ref):
    letters = re.match(r'[A-Za-z]+', ref or '')
    if not letters:
        return None
    index = 0
    for char in letters.group().upper():
        index = index * 26 + ord(char) - 64
    return index - 1


def _sheet_rows(root, strings):
    """Tab-joined text of each non-empty row of a worksheet; an empty cell keeps the columns after it aligned."""
    for row in root.iter():
        if _local(row.tag) != 'row':
            continue
        values = []
        for cell in row:
            if _local(cell.tag) != 'c':
                continue
            kind = cell.get('t')
            value = ''
            if kind == 'inlineStr':
                value = ''.join(node.text or '' for node in cell.iter() if _local(node.tag) == 't')
            else:
                raw = next((node.text or '' for node in cell if _local(node.tag) == 'v'), '')
                if kind == 's':
                    position = int(raw) if raw.strip().isdigit() else -1
                    if not 0 <= position < len(strings):
                        raise DocumentError('Could not extract text from this XLSX.')
                    value = strings[position]
                else:
                    value = raw
            column = _column_index(cell.get('r'))
            if column is None:
                column = len(values)
            if column >= XLSX_MAX_COLUMNS:
                raise DocumentError(f'XLSX over {XLSX_MAX_COLUMNS} columns.')
            values += [''] * (column - len(values))
            if column == len(values):
                values.append(value)
            else:
                values[column] = value
        if any(value.strip() for value in values):
            yield '\t'.join(values)


def extract_xlsx(data):
    """Text of an .xlsx: each sheet, in workbook order, under `[sheet NAME]`, then one line per row with the cells
    separated by tabs. Shared strings are resolved; numbers and inline strings are kept; a formula contributes its cached
    value, nothing is calculated.

    Parameters: data (the file bytes). Returns: UTF-8 text bytes. Raises: DocumentError with a readable reason for a
    corrupt, encrypted, oversized or hostile package, a workbook over XLSX_MAX_ROWS rows or MAX_TEXT_BYTES of text, or
    one without any text."""
    with _open_package(data, 'XLSX', 'xl/workbook.xml') as package:
        workbook = package.parse('xl/workbook.xml')
        targets = {}
        if 'xl/_rels/workbook.xml.rels' in package.entries:
            for rel in package.parse('xl/_rels/workbook.xml.rels'):
                target = rel.get('Target', '')
                targets[rel.get('Id')] = posixpath.normpath(target.lstrip('/') if target.startswith('/')
                                                            else posixpath.join('xl', target))
        strings = _shared_strings(package)
        lines = _Lines('XLSX')
        rows = 0
        for sheet in (node for node in workbook.iter() if _local(node.tag) == 'sheet'):
            rid = next((value for key, value in sheet.attrib.items() if _local(key) == 'id'), None)
            part = targets.get(rid)
            if part not in package.entries:
                raise DocumentError('Invalid XLSX or excessive structure.')
            header = f"[sheet {' '.join(sheet.get('name', '').split())}]"
            for line in _sheet_rows(package.parse(part), strings):
                rows += 1
                if rows > XLSX_MAX_ROWS:
                    raise DocumentError(f'XLSX over {XLSX_MAX_ROWS} rows.')
                if header:
                    lines.add(header)
                    header = ''
                lines.add(line)
    if not lines:
        raise DocumentError('XLSX has no text.')
    return ('\n'.join(lines) + '\n').encode('utf-8')


PDF_SCRIPT = """
import io, json, sys
import pypdf
reader = pypdf.PdfReader(io.BytesIO(sys.stdin.buffer.read()))
if reader.is_encrypted and reader.decrypt("") == pypdf.PasswordType.NOT_DECRYPTED:
    print(json.dumps({"encrypted": True}))
    sys.exit()
limit, budget = int(sys.argv[1]), int(sys.argv[2])
pages, size = [], 0
for page in reader.pages[:limit]:
    pages.append((page.extract_text() or "").strip()[:budget - size])
    size += len(pages[-1])
    if size >= budget:
        break
print(json.dumps({"pages": pages, "count": len(reader.pages)}))
"""


def run_pdf(data, timeout, max_pages, max_chars):
    """Text of a PDF (pypdf, pure Python), page by page, with `[page N]` markers. A hostile PDF can make pypdf parse for
    minutes, so it runs in an isolated subprocess (`-I`) killed at the deadline.

    Parameters: data (PDF bytes), timeout (seconds before the subprocess is killed), max_pages, max_chars (cap on the
    text read, over all pages). Returns: the marked text, with a `[pages A-B not read]` line when a cap cut it; empty
    pages are left out. Raises: PdfTimeout, PdfEncrypted (a password is needed), PdfUnreadable (corrupt; `detail` has
    the last line pypdf reported), all DocumentError."""
    try:
        done = subprocess.run([sys.executable, "-I", "-c", PDF_SCRIPT, str(max_pages), str(max_chars)],
                              input=data, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise PdfTimeout(f"PDF not read in {timeout:.0f}s.") from None
    if done.returncode:
        detail = (done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1]
        raise PdfUnreadable("PDF could not be read.", detail)
    try:
        result = json.loads(done.stdout)
        if result.get("encrypted"):
            raise PdfEncrypted("PDF protected by a password.")
        pages, count = result["pages"], result["count"]
    except (ValueError, KeyError, AttributeError) as exc:
        if isinstance(exc, PdfEncrypted):
            raise
        raise PdfUnreadable("PDF could not be read.", f"unexpected extractor output ({type(exc).__name__})") from None
    text = "\n\n".join(f"[page {number}]\n{page}" for number, page in enumerate(pages, 1) if page)
    if count > len(pages):
        text += f"\n\n[pages {len(pages) + 1}-{count} not read]"
    return text


def extract_pdf(data):
    """Indexable text of a PDF through run_pdf with the indexer's deadline and caps.

    Parameters: data (PDF bytes). Returns: UTF-8 bytes of the marked text. Raises: DocumentError with a readable reason
    for a timeout, a password, a corrupt file or a PDF without a text layer (scanned; there is no OCR)."""
    try:
        text = run_pdf(data, PDF_TIMEOUT_S, PDF_MAX_PAGES, PDF_MAX_CHARS)
    except PdfUnreadable as exc:
        raise PdfUnreadable(f"PDF could not be read ({exc.detail})"[:200], exc.detail) from None
    if "[page " not in text:
        raise DocumentError("PDF has no text layer (scanned pages are not read).")
    return (text + "\n").encode("utf-8", "replace")


_EXTENSIONS = ('.docx', '.pptx', '.xlsx', '.pdf')


def is_extracted(path):
    """Whether the file type goes through a text extractor before indexing.

    Parameters: path (any file name or path). Returns: True for `.docx`, `.pptx`, `.xlsx` and `.pdf`, case-insensitive;
    macro and binary formats (`.xlsm`, `.xlsb`, `.xls`, `.ppt`, `.pptm`) are not extracted."""
    return str(path).lower().endswith(_EXTENSIONS)


def size_limit(path):
    """Largest file size, in bytes, the extractor of this type accepts.

    Parameters: path of an extracted type (see is_extracted). Returns: the byte limit of its extractor."""
    return PDF_MAX_BYTES if str(path).lower().endswith('.pdf') else MAX_ARCHIVE_BYTES


def extract(path, data):
    """Text of an extracted-type file.

    Parameters: path (decides the type, see is_extracted), data (file bytes). Returns: UTF-8 text bytes. Raises:
    DocumentError with a readable reason."""
    name = str(path).lower()
    if name.endswith('.pdf'):
        return extract_pdf(data)
    if name.endswith('.pptx'):
        return extract_pptx(data)
    if name.endswith('.xlsx'):
        return extract_xlsx(data)
    return extract_docx(data)


def location_kind(path):
    """Kind of line numbers a result of this file carries.

    Parameters: path. Returns: `extracted_text_line` (lines of the extracted text, not of the file) for extracted
    types, else `source_line`."""
    return 'extracted_text_line' if is_extracted(path) else 'source_line'
