"""Deterministic document fixture builders (PDF/DOCX/EPUB) for tests and benchmarks.

These builders write small but structurally valid documents with only the Python
standard library so extractors can be exercised offline without network access
or third-party writers.
"""

from __future__ import annotations

from pathlib import Path
import zipfile


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_text_pdf(pages: list[list[str]], *, title: str = "") -> bytes:
    """Return bytes of a valid multi-page PDF whose text layer contains the given lines."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    catalog_id = add(b"")  # placeholder, filled later
    pages_id = add(b"")
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "72 760 Td"]
        for line in lines:
            ops.append(f"({_pdf_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", errors="replace")
        content_id = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_id = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font_id, content_id)
        )
        page_ids.append(page_id)
    kids = " ".join(f"{pid} 0 R" for pid in page_ids).encode()
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_ids)
    objects[catalog_id - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    info_id = None
    if title:
        info_id = add(b"<< /Title (" + _pdf_escape(title).encode("latin-1", errors="replace") + b") >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog_id)
    if info_id:
        trailer += b" /Info %d 0 R" % info_id
    trailer += b" >>"
    out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)


def write_text_pdf(path: str | Path, pages: list[list[str]], *, title: str = "") -> Path:
    target = Path(path)
    target.write_bytes(build_text_pdf(pages, title=title))
    return target


def write_docx(path: str | Path, paragraphs: list[tuple[str, str]], table: list[list[str]] | None = None) -> Path:
    """Write a minimal DOCX. ``paragraphs`` is a list of (style, text); style '' for body text."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def esc(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{esc(text)}</w:t></w:r></w:p>")
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{esc(c)}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
            for row in table
        )
        body.append(f"<w:tbl>{rows}</w:tbl>")
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{"".join(body)}</w:body></w:document>'
    target = Path(path)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        zf.writestr("word/document.xml", document)
    return target


def write_epub(path: str | Path, title: str, chapters: list[tuple[str, list[str]]]) -> Path:
    """Write a minimal EPUB 3 container with one XHTML file per chapter."""
    target = Path(path)
    manifest = []
    spine = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        for index, (heading, paras) in enumerate(chapters, start=1):
            body = f"<h1>{heading}</h1>" + "".join(f"<p>{p}</p>" for p in paras)
            zf.writestr(
                f"OEBPS/ch{index}.xhtml",
                f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{heading}</title></head><body>{body}</body></html>',
            )
            manifest.append(f'<item id="ch{index}" href="ch{index}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{index}"/>')
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>'
            f'<manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>',
        )
    return target
