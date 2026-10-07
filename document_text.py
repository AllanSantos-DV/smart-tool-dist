"""Extração local de texto DOCX, sem Office, execução de macros ou acesso externo."""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

VERSION = 1
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


class DocumentError(ValueError):
    pass


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


def location_kind(path):
    return 'extracted_text_line' if str(path).lower().endswith('.docx') else 'source_line'
