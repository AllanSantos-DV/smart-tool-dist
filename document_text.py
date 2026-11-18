"""Extração local de texto DOCX, PPTX, XLSX e PDF, sem Office, execução de macros ou acesso externo."""
import contextlib
import csv
import io
import json
import posixpath
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from urllib.parse import unquote

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

    def read(self, name):
        """The bytes of one part, charged to the XML budget; raises DocumentError for an encrypted or oddly compressed
        part and for one that would pass the budget."""
        info = self.entries[name]
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise DocumentError(f'Encrypted {self.label} or unsupported compression.')
        if info.file_size > MAX_XML_BYTES - self.xml_bytes:
            raise DocumentError(f'{self.label} XML content over the limit.')
        with self.archive.open(info) as stream:
            raw = stream.read(MAX_XML_BYTES - self.xml_bytes + 1)
        self.xml_bytes += len(raw)
        if self.xml_bytes > MAX_XML_BYTES:
            raise DocumentError(f'{self.label} contains excessive XML or disallowed entities.')
        return raw

    def parse(self, name):
        raw = self.read(name)
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
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


def _docx_namespace(root):
    """The WordprocessingML namespace prefix (transitional or strict) of a parsed part."""
    return W if root.tag.startswith(W) else '{http://purl.oclc.org/ooxml/wordprocessingml/main}'


def _docx_text(paragraph, namespace):
    """Text of one `w:p`: runs, tabs and line breaks; a nested paragraph (text box) is not part of it."""
    pending = list(reversed(paragraph))
    pieces = []
    while pending:
        child = pending.pop()
        if child.tag == namespace + 'p':
            continue
        if child.tag == namespace + 't':
            pieces.append(child.text or '')
        elif child.tag == namespace + 'tab':
            pieces.append('\t')
        elif child.tag in (namespace + 'br', namespace + 'cr'):
            pieces.append('\n')
        else:
            pending.extend(reversed(child))
    return ''.join(pieces)


def _docx_extra_parts(package):
    """Names of the header, footer, footnote and endnote parts of a .docx, in a stable order."""
    return sorted(name for name in package.entries if re.fullmatch(
        r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml', name))


def extract_docx(data):
    """Text of a .docx: body, headers, footers, footnotes and endnotes, one line per non-empty paragraph.

    Parameters: data (the file bytes). Returns: UTF-8 text bytes. Raises: DocumentError with a readable reason for a
    corrupt, encrypted or oversized package, or one with a DOCTYPE or entity."""
    with _open_package(data, 'DOCX', 'word/document.xml') as package:
        paragraphs = _Lines('DOCX')
        for name in ['word/document.xml'] + _docx_extra_parts(package):
            root = package.parse(name)
            namespace = _docx_namespace(root)
            for paragraph in root.iter(namespace + 'p'):
                text = _docx_text(paragraph, namespace)
                if text.strip():
                    paragraphs.add(text)
        return ('\n'.join(paragraphs) + '\n' if paragraphs else '').encode('utf-8')


def _docx_heading_levels(package, namespace):
    """`{styleId: level}` of the heading styles of word/styles.xml (the style name is `heading N` in every language,
    `Title` is level 1); empty when the part is missing."""
    if 'word/styles.xml' not in package.entries:
        return {}
    levels = {}
    for style in package.parse('word/styles.xml').iter(namespace + 'style'):
        name = next((node.get(namespace + 'val', '') for node in style if node.tag == namespace + 'name'), '').lower()
        found = re.fullmatch(r'heading (\d)', name)
        if found or name == 'title':
            levels[style.get(namespace + 'styleId')] = int(found.group(1)) if found else 1
    return levels


def _docx_paragraph_line(paragraph, namespace, levels):
    """Markdown line of a paragraph: `#` heading by style or outline level, `- ` item (indented by level) for a list
    paragraph, else the plain text; an empty string for a paragraph without text."""
    text = ' '.join(_docx_text(paragraph, namespace).split())
    if not text:
        return ''
    properties = next((node for node in paragraph if node.tag == namespace + 'pPr'), None)
    if properties is None:
        return text
    style = next((node.get(namespace + 'val') for node in properties if node.tag == namespace + 'pStyle'), None)
    outline = next((node.get(namespace + 'val') for node in properties if node.tag == namespace + 'outlineLvl'), None)
    level = levels.get(style) or (int(outline) + 1 if outline and outline.isdigit() and int(outline) < 6 else 0)
    if level:
        return '#' * min(level, 6) + ' ' + text
    numbering = next((node for node in properties if node.tag == namespace + 'numPr'), None)
    if numbering is not None:
        depth = next((node.get(namespace + 'val') for node in numbering if node.tag == namespace + 'ilvl'), '0')
        return '  ' * (int(depth) if depth.isdigit() else 0) + '- ' + text
    return text


def _docx_table_lines(table, namespace):
    """GFM table lines of a `w:tbl`: one row per `w:tr`, the first row as the header, cell text on one line."""
    rows = []
    for row in table:
        if row.tag != namespace + 'tr':
            continue
        cells = []
        for cell in row:
            if cell.tag == namespace + 'tc':
                text = ' '.join(' '.join(_docx_text(p, namespace).split()) for p in cell.iter(namespace + 'p'))
                cells.append(' '.join(text.split()).replace('|', '\\|'))
        if any(cells):
            rows.append('| ' + ' | '.join(cells) + ' |')
    if len(rows) > 1:
        rows.insert(1, '|' + ' --- |' * (rows[0].count(' | ') + 1))
    return rows


def _docx_body_items(node, namespace):
    """The paragraphs and tables of a body (or of a content control inside it) in document order."""
    for child in node:
        if child.tag in (namespace + 'p', namespace + 'tbl'):
            yield child
        elif child.tag == namespace + 'sdt':
            for content in child:
                if content.tag == namespace + 'sdtContent':
                    yield from _docx_body_items(content, namespace)


def docx_blocks(data):
    """Markdown blocks of a .docx in document order, for reading: headings as `#`, list items as `- `, tables as GFM
    tables, other paragraphs as text. Footnotes, endnotes and the distinct header and footer lines follow under
    `## Notes` and `## Headers and footers`.

    Parameters: data (the file bytes). Returns: a list of blocks (a block is a paragraph line or a whole table, lines
    joined by newline). Raises: DocumentError with a readable reason for a corrupt, encrypted, oversized or hostile
    package, or one without text."""
    with _open_package(data, 'DOCX', 'word/document.xml') as package:
        root = package.parse('word/document.xml')
        namespace = _docx_namespace(root)
        levels = _docx_heading_levels(package, namespace)
        blocks = _Lines('DOCX')
        body = next((node for node in root if node.tag == namespace + 'body'), root)
        for item in _docx_body_items(body, namespace):
            if item.tag == namespace + 'tbl':
                table = _docx_table_lines(item, namespace)
                if table:
                    blocks.add('\n'.join(table))
            else:
                line = _docx_paragraph_line(item, namespace, levels)
                if line:
                    blocks.add(line)
        notes, frame = [], []
        for name in _docx_extra_parts(package):
            part = package.parse(name)
            target = frame if re.search(r'header|footer', name) else notes
            for paragraph in part.iter(_docx_namespace(part) + 'p'):
                text = ' '.join(_docx_text(paragraph, _docx_namespace(part)).split())
                if text and text not in target:
                    target.append(text)
        if notes:
            blocks.add('## Notes', *notes)
        if frame:
            blocks.add('## Headers and footers', *frame)
    if not blocks:
        raise DocumentError('DOCX has no text.')
    return list(blocks)


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


def pptx_slides(data):
    """The slides of a .pptx in slide order, for extraction and for reading.

    Parameters: data (the file bytes). Returns: a list of `(number, body lines, notes lines)`, a slide without text
    included with empty lists. Raises: DocumentError with a readable reason for a corrupt, encrypted, oversized or hostile
    package or one over MAX_TEXT_BYTES of text."""
    def has_slides(names):
        return any(re.fullmatch(r'ppt/slides/slide\d+\.xml', name) for name in names)

    with _open_package(data, 'PPTX', has_slides) as package:
        numbered = sorted((int(m.group(1)), name) for name in package.entries
                          if (m := re.fullmatch(r'ppt/slides/slide(\d+)\.xml', name)))
        budget = _Lines('PPTX')
        slides = []
        for number, name in numbered:
            body = _drawing_paragraphs(package.parse(name))
            notes_name = f'ppt/notesSlides/notesSlide{number}.xml'
            notes = _drawing_paragraphs(package.parse(notes_name)) if notes_name in package.entries else []
            budget.add(*body, *notes)
            slides.append((number, body, notes))
    return slides


def extract_pptx(data):
    """Text of a .pptx: each slide under `[slide N]`, then its speaker notes under `[notes N]`, in slide order.

    Parameters: data (the file bytes). Returns: UTF-8 text bytes. Raises: DocumentError with a readable reason for a
    corrupt, encrypted, oversized or hostile package, or a deck without any text."""
    lines = _Lines('PPTX')
    for number, body, notes in pptx_slides(data):
        if body:
            lines.add(f'[slide {number}]', *body)
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


def xlsx_sheets(data):
    """The sheets of an .xlsx in workbook order, for extraction and for reading. Shared strings are resolved; numbers
    and inline strings are kept; a formula contributes its cached value, nothing is calculated.

    Parameters: data (the file bytes). Returns: a list of `(name, rows)`, `rows` being the tab-joined text of each
    non-empty row (empty for a sheet without text). Raises: DocumentError with a readable reason for a corrupt,
    encrypted, oversized or hostile package, or a workbook over XLSX_MAX_ROWS rows or MAX_TEXT_BYTES of text."""
    with _open_package(data, 'XLSX', 'xl/workbook.xml') as package:
        workbook = package.parse('xl/workbook.xml')
        targets = {}
        if 'xl/_rels/workbook.xml.rels' in package.entries:
            for rel in package.parse('xl/_rels/workbook.xml.rels'):
                target = rel.get('Target', '')
                targets[rel.get('Id')] = posixpath.normpath(target.lstrip('/') if target.startswith('/')
                                                            else posixpath.join('xl', target))
        strings = _shared_strings(package)
        budget = _Lines('XLSX')
        sheets = []
        total = 0
        for sheet in (node for node in workbook.iter() if _local(node.tag) == 'sheet'):
            rid = next((value for key, value in sheet.attrib.items() if _local(key) == 'id'), None)
            part = targets.get(rid)
            if part not in package.entries:
                raise DocumentError('Invalid XLSX or excessive structure.')
            name = ' '.join(sheet.get('name', '').split())
            rows = []
            for line in _sheet_rows(package.parse(part), strings):
                total += 1
                if total > XLSX_MAX_ROWS:
                    raise DocumentError(f'XLSX over {XLSX_MAX_ROWS} rows.')
                if not rows:
                    budget.add(f'[sheet {name}]')
                budget.add(line)
                rows.append(line)
            sheets.append((name, rows))
    return sheets


def extract_xlsx(data):
    """Text of an .xlsx: each sheet, in workbook order, under `[sheet NAME]`, then one line per row with the cells
    separated by tabs. Shared strings are resolved; numbers and inline strings are kept; a formula contributes its cached
    value, nothing is calculated.

    Parameters: data (the file bytes). Returns: UTF-8 text bytes. Raises: DocumentError with a readable reason for a
    corrupt, encrypted, oversized or hostile package, a workbook over XLSX_MAX_ROWS rows or MAX_TEXT_BYTES of text, or
    one without any text."""
    lines = _Lines('XLSX')
    for name, rows in xlsx_sheets(data):
        if rows:
            lines.add(f'[sheet {name}]', *rows)
    if not lines:
        raise DocumentError('XLSX has no text.')
    return ('\n'.join(lines) + '\n').encode('utf-8')


_SKIPPED_HTML = frozenset(('script', 'style', 'noscript', 'template', 'svg', 'head'))
_BLOCK_HTML = frozenset(('p', 'div', 'br', 'section', 'article', 'header', 'footer', 'main', 'aside', 'nav', 'ul', 'ol',
                         'blockquote', 'pre', 'hr', 'figure', 'figcaption', 'dl', 'dt', 'dd', 'table', 'body', 'form',
                         'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'))
_CHARSET = re.compile(rb'charset\s*=\s*["\']?([\w.:-]+)', re.I)


def _decode_text(data, label, declared=False):
    """Text of a file's bytes: UTF-8 (a BOM is dropped), UTF-16 with a BOM, or, for markup, the charset it declares in
    its first 2 KiB. A file that is none of those is refused, never decoded into wrong characters."""
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        found = _CHARSET.search(data[:2048]) if declared else None
        if found:
            try:
                return data.decode(found.group(1).decode('ascii'))
            except (LookupError, UnicodeDecodeError):
                pass
    raise DocumentError(f'{label} is not UTF-8 and declares no encoding the reader can use.')


class _HtmlText(HTMLParser):
    """Markdown blocks of an HTML document: headings as `#`, list items as `- `, table rows as `a | b`, the other text
    as lines. No tree is built and nothing is fetched or executed; script, style and the head except its title are
    skipped. Output goes through a _Lines budget, so a page over MAX_TEXT_BYTES of text is refused."""

    def __init__(self, label):
        super().__init__(convert_charrefs=True)
        self.blocks = _Lines(label)
        self.pieces, self.prefix, self.skipped = [], '', 0
        self.row, self.cell, self.title, self.in_title = [], 0, [], False

    def _flush(self):
        text = ' '.join(''.join(self.pieces).split())
        self.pieces = []
        if text:
            self.blocks.add(self.prefix + text)
        self.prefix = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            self.skipped = 0
        if tag == 'title':
            self.in_title = not self.title
        elif tag in _SKIPPED_HTML:
            self.skipped += 1
        elif self.skipped:
            return
        elif tag in ('td', 'th'):
            self.cell += 1
        elif tag == 'tr':
            self._flush()
        elif tag in _BLOCK_HTML and not self.cell:
            self._flush()
            if tag[0] == 'h' and len(tag) == 2:
                self.prefix = '#' * int(tag[1]) + ' '
            elif tag == 'li':
                self.prefix = '- '

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        elif tag in _SKIPPED_HTML:
            self.skipped = max(0, self.skipped - 1)
        elif self.skipped:
            return
        elif tag in ('td', 'th') and self.cell:
            self.cell -= 1
            self.row.append(' '.join(''.join(self.pieces).split()))
            self.pieces = []
        elif tag == 'tr':
            if any(self.row):
                self.blocks.add(' | '.join(self.row))
            self.row = []
        elif tag in _BLOCK_HTML and not self.cell:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.skipped:
            self.pieces.append(data)

    def result(self):
        self.close()
        self._flush()
        title = ' '.join(''.join(self.title).split())
        return ([f'# {title}'] if title else []) + list(self.blocks)


def _html_blocks(data, label):
    parser = _HtmlText(label)
    parser.feed(_decode_text(data, label, declared=True))
    return parser.result()


def html_blocks(data):
    """Markdown blocks of an .html or .htm file in document order, for reading: the title as `#`, headings as `#`
    levels, list items as `- `, table rows as `a | b`, other text as lines. Scripts, styles and the rest of the head
    are not text; a link is its text.

    Parameters: data (the file bytes). Returns: a list of blocks. Raises: DocumentError with a readable reason for an
    encoding the reader cannot use, a page over MAX_TEXT_BYTES of text, or one without text."""
    blocks = _html_blocks(data, 'HTML')
    if not blocks:
        raise DocumentError('HTML has no text.')
    return blocks


def csv_rows(data):
    """The rows of a .csv file, for reading: one tab-joined line per non-empty row (the delimiter is `,`, `;`, tab or
    `|`, found from the first 4 KiB), a line break inside a cell turned into a space.

    Parameters: data (the file bytes). Returns: a list of lines. Raises: DocumentError with a readable reason for an
    encoding the reader cannot use, malformed quoting, a file over XLSX_MAX_ROWS rows or MAX_TEXT_BYTES of text, or one
    without text."""
    text = _decode_text(data, 'CSV')
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    rows = _Lines('CSV')
    try:
        for record in csv.reader(io.StringIO(text, newline=''), dialect):
            values = [' '.join(cell.split()) for cell in record]
            if any(values):
                if len(rows) >= XLSX_MAX_ROWS:
                    raise DocumentError(f'CSV over {XLSX_MAX_ROWS} rows.')
                rows.add('\t'.join(values))
    except csv.Error as exc:
        raise DocumentError(f'Could not read this CSV: {exc}.') from None
    if not rows:
        raise DocumentError('CSV has no text.')
    return list(rows)


def _epub_chapter_paths(package):
    """Zip names of the XHTML parts of an EPUB in reading order: the package document the container names, its spine
    resolved through the manifest. A part outside the archive or a path leaving it is refused."""
    container = package.parse('META-INF/container.xml')
    rootfile = next((node.get('full-path') for node in container.iter() if _local(node.tag) == 'rootfile'), None)
    opf = posixpath.normpath((rootfile or '').lstrip('/'))
    if not rootfile or opf.startswith('..') or opf not in package.entries:
        raise DocumentError('Invalid EPUB or excessive structure.')
    package_root = package.parse(opf)
    manifest = {node.get('id'): node for node in package_root.iter() if _local(node.tag) == 'item'}
    base = posixpath.dirname(opf)
    paths = []
    for itemref in (node for node in package_root.iter() if _local(node.tag) == 'itemref'):
        item = manifest.get(itemref.get('idref'))
        if item is None or item.get('media-type') not in ('application/xhtml+xml', 'text/html'):
            continue
        part = posixpath.normpath(posixpath.join(base, unquote(item.get('href', '')).split('#')[0]))
        if part.startswith('..') or part not in package.entries:
            raise DocumentError('Invalid EPUB or excessive structure.')
        paths.append(part)
    return paths


def epub_chapters(data):
    """The chapters of an .epub in reading order, for reading. Each XHTML part of the spine is read as HTML (see
    html_blocks); the package is opened with the same guards as the Office formats.

    Parameters: data (the file bytes). Returns: a list of block lists, one per chapter (a chapter without text is an
    empty list). Raises: DocumentError with a readable reason for a corrupt, encrypted, oversized or hostile package, an
    encoding the reader cannot use, or a book without any text."""
    with _open_package(data, 'EPUB', 'META-INF/container.xml') as package:
        chapters = [_html_blocks(package.read(path), 'EPUB') for path in _epub_chapter_paths(package)]
    if not any(chapters):
        raise DocumentError('EPUB has no text.')
    return chapters


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


def pdf_pages(data, timeout, max_pages, max_chars):
    """Text of each page of a PDF (pypdf, pure Python). A hostile PDF can make pypdf parse for minutes, so it runs in an
    isolated subprocess (`-I`) killed at the deadline.

    Parameters: data (PDF bytes), timeout (seconds before the subprocess is killed), max_pages, max_chars (cap on the
    text read, over all pages). Returns: `(pages, count)`: the text of the pages read (an empty string for a page
    without text) and the page count of the file, which is larger when a cap cut the reading. Raises: PdfTimeout,
    PdfEncrypted (a password is needed), PdfUnreadable (corrupt; `detail` has the last line pypdf reported), all
    DocumentError."""
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
        return result["pages"], result["count"]
    except (ValueError, KeyError, AttributeError) as exc:
        if isinstance(exc, PdfEncrypted):
            raise
        raise PdfUnreadable("PDF could not be read.", f"unexpected extractor output ({type(exc).__name__})") from None


def run_pdf(data, timeout, max_pages, max_chars):
    """Text of a PDF through pdf_pages, with `[page N]` markers.

    Parameters: as pdf_pages. Returns: the marked text, with a `[pages A-B not read]` line when a cap cut it; empty
    pages are left out. Raises: as pdf_pages."""
    pages, count = pdf_pages(data, timeout, max_pages, max_chars)
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


_EXTENSIONS = ('.docx', '.pptx', '.xlsx', '.xlsm', '.pdf')


def is_extracted(path):
    """Whether the file type goes through a text extractor before indexing.

    Parameters: path (any file name or path). Returns: True for `.docx`, `.pptx`, `.xlsx`, `.xlsm` and `.pdf`,
    case-insensitive; an `.xlsm` is read as an `.xlsx` (its XML parts only, no macro runs); the binary and other macro
    formats (`.xlsb`, `.xls`, `.ppt`, `.pptm`) are not extracted."""
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
    if name.endswith(('.xlsx', '.xlsm')):
        return extract_xlsx(data)
    return extract_docx(data)


def location_kind(path):
    """Kind of line numbers a result of this file carries.

    Parameters: path. Returns: `extracted_text_line` (lines of the extracted text, not of the file) for extracted
    types, else `source_line`."""
    return 'extracted_text_line' if is_extracted(path) else 'source_line'
