"""Extração local de texto DOCX e PDF, sem Office, execução de macros ou acesso externo."""
import io
import json
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

VERSION = 2
PDF_MAX_BYTES = 20 * 1024 * 1024
PDF_TIMEOUT_S = 20
PDF_MAX_PAGES = 300
PDF_MAX_CHARS = 1_000_000
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
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
    def doctype(self, name, pubid, system):
        # O callback também recusa DTD em XML UTF-16, antes de expandir entidades.
        raise DocumentError('DOCX contains disallowed entities.')


def extract_docx(data):
    if len(data)>MAX_ARCHIVE_BYTES:
        raise DocumentError('DOCX over the 10 MiB limit.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            listing=archive.infolist()
            entries={info.filename:info for info in listing}
            if len(listing)>2000 or 'word/document.xml' not in entries:
                raise DocumentError('Invalid DOCX or excessive structure.')
            names=['word/document.xml']+sorted(name for name in entries if re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml',name))
            paragraphs=[];xml_bytes=0;text_bytes=0
            for name in names:
                info=entries[name]
                if info.flag_bits&1 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                    raise DocumentError('Encrypted DOCX or unsupported compression.')
                if info.file_size>MAX_XML_BYTES-xml_bytes:
                    raise DocumentError('DOCX XML content over the limit.')
                with archive.open(info) as stream:
                    raw=stream.read(MAX_XML_BYTES-xml_bytes+1)
                xml_bytes+=len(raw)
                if xml_bytes>MAX_XML_BYTES or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise DocumentError('DOCX contains excessive XML or disallowed entities.')
                root=ET.fromstring(raw,parser=ET.XMLParser(target=_DocumentTreeBuilder()))
                namespace=W if root.tag.startswith(W) else '{http://purl.oclc.org/ooxml/wordprocessingml/main}'
                for paragraph in root.iter(namespace+'p'):
                    def parts(node):
                        pending=list(reversed(node))
                        while pending:
                            child=pending.pop()
                            if child.tag==namespace+'p':continue
                            if child.tag==namespace+'t':yield child.text or ''
                            elif child.tag==namespace+'tab':yield '\t'
                            elif child.tag in (namespace+'br',namespace+'cr'):yield '\n'
                            else:pending.extend(reversed(child))
                    text=''.join(parts(paragraph))
                    if text.strip():
                        text_bytes+=len(text.encode('utf-8'))+1
                        if text_bytes>MAX_TEXT_BYTES:raise DocumentError('Text extracted from DOCX exceeds 1 MiB.')
                        paragraphs.append(text)
            return ('\n'.join(paragraphs)+'\n' if paragraphs else '').encode('utf-8')
    except (zipfile.BadZipFile,ET.ParseError,RuntimeError,NotImplementedError,OSError) as exc:
        raise DocumentError('Could not extract text from this DOCX.') from exc


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


def is_extracted(path):
    """Whether the file type goes through a text extractor before indexing.

    Parameters: path (any file name or path). Returns: True for `.docx` and `.pdf`, case-insensitive."""
    return str(path).lower().endswith(('.docx', '.pdf'))


def size_limit(path):
    """Largest file size, in bytes, the extractor of this type accepts.

    Parameters: path of an extracted type (see is_extracted). Returns: the byte limit of its extractor."""
    return PDF_MAX_BYTES if str(path).lower().endswith('.pdf') else MAX_ARCHIVE_BYTES


def extract(path, data):
    """Text of an extracted-type file.

    Parameters: path (decides the type, see is_extracted), data (file bytes). Returns: UTF-8 text bytes. Raises:
    DocumentError with a readable reason."""
    return extract_pdf(data) if str(path).lower().endswith('.pdf') else extract_docx(data)


def location_kind(path):
    """Kind of line numbers a result of this file carries.

    Parameters: path. Returns: `extracted_text_line` (lines of the extracted text, not of the file) for extracted
    types, else `source_line`."""
    return 'extracted_text_line' if is_extracted(path) else 'source_line'
