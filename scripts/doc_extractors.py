# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = ["pypdf", "beautifulsoup4"]
# ///
"""Deterministic, model-free local document extractors.

Supported inputs: ``.pdf`` (per-page text, outline/heuristic headings, and
``[page N]`` UTF-8 byte-span coordinates), ``.docx`` (OOXML paragraphs, heading
styles, tables), ``.epub`` (OCF container + OPF spine order), local ``.html`` /
``.htm`` (no network fetches), and ``.csv`` / ``.tsv`` (``[row N]`` records).

Every extractor returns UTF-8 Markdown with ``\\n`` line endings and stable
section anchors so ``scripts/evidence.py`` can byte-anchor verbatim quotes in
the emitted extraction artifact. Raw binaries are never used as evidence
sources directly; callers archive them separately with SHA-256 lineage.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import io
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import _is_reparse_or_link  # noqa: E402

DOC_EXTRACTOR_EXTS = frozenset({".pdf", ".docx", ".epub", ".html", ".htm", ".csv", ".tsv"})
MAX_DOC_BINARY_BYTES = 50 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_RATIO = 200
MAX_PDF_PAGES = 2000
MAX_TABLE_ROWS = 20000
MAX_EXTRACTED_CHARS = 8_000_000

_WS_RE = re.compile(r"[ \t\u00a0\u2000-\u200b\u3000]+")
_HEADING_NAME_RE = re.compile(
    r"^(?:abstract|introduction|background|related work|method(?:s|ology)?|approach|design|"
    r"implementation|evaluation|results|discussion|limitations|conclusions?|future work|"
    r"acknowledg(?:e)?ments?|references|bibliography|appendix(?:\s+[a-z0-9]+)?)$",
    re.IGNORECASE,
)
_NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+){0,3}\.?|[IVXLC]+\.)\s+[A-Z][^.!?]{1,90}$")

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentExtractionError(ValueError):
    """Raised when a document cannot be extracted safely and deterministically."""


@dataclass
class DocumentExtraction:
    markdown: str
    extractor: str
    model: str
    model_digest: str
    limitations: str
    title: str = ""
    page_count: int = 0
    section_count: int = 0
    row_count: int = 0
    page_spans: list[dict[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def provenance(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "extractor": self.extractor,
            "model": self.model,
            "model_digest": self.model_digest,
            "limitations": self.limitations,
        }
        if self.title:
            meta["document_title"] = self.title
        if self.page_count:
            meta["page_count"] = self.page_count
        if self.section_count:
            meta["section_count"] = self.section_count
        if self.row_count:
            meta["row_count"] = self.row_count
        return meta


# ─── Shared helpers ──────────────────────────────────────────────────────────

def _clean_line(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return _WS_RE.sub(" ", text).strip()


def _neutralize(text: str) -> str:
    """Prevent extracted document text from forging capture provenance comments or frontmatter."""
    out = text.replace("<!--", "&lt;!--")
    return re.sub(r"(?m)^---\s*$", "- - -", out)


def _finalize(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    if len(text) > MAX_EXTRACTED_CHARS:
        raise DocumentExtractionError(f"Extracted text exceeds {MAX_EXTRACTED_CHARS} characters.")
    return text


def validate_document_source(path: str | Path, *, max_bytes: int = MAX_DOC_BINARY_BYTES) -> int:
    target = str(path)
    if os.path.lexists(target) and _is_reparse_or_link(target):
        raise PermissionError(f"Refusing to extract a symlink or reparse-point document: {target}")
    if not os.path.isfile(target):
        raise FileNotFoundError(f"Document not found: {target}")
    size = os.path.getsize(target)
    if size <= 0:
        raise DocumentExtractionError(f"Document is empty: {target}")
    if size > max_bytes:
        raise DocumentExtractionError(f"Document exceeds maximum allowed size ({max_bytes} bytes): {target}")
    return size


def _open_safe_zip(path: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise DocumentExtractionError(f"Not a valid ZIP-based document: {exc}") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        archive.close()
        raise DocumentExtractionError("ZIP container has too many entries (possible zip bomb).")
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in PurePosixPath(name.replace("\\", "/")).parts:
            archive.close()
            raise DocumentExtractionError(f"Unsafe path inside ZIP container: {name!r}")
        total += info.file_size
        if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_ZIP_RATIO and info.file_size > 1_000_000:
            archive.close()
            raise DocumentExtractionError("ZIP entry compression ratio is unsafe (possible zip bomb).")
    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
        archive.close()
        raise DocumentExtractionError("ZIP container uncompressed size exceeds the safety limit.")
    return archive


def _safe_xml(data: bytes) -> ET.Element:
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise DocumentExtractionError("XML DOCTYPE/ENTITY declarations are not allowed in document containers.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentExtractionError(f"Malformed XML inside document container: {exc}") from exc


# ─── PDF ─────────────────────────────────────────────────────────────────────

def _pdf_outline_titles(reader) -> set[str]:
    titles: set[str] = set()

    def walk(items) -> None:
        for item in items or []:
            if isinstance(item, list):
                walk(item)
                continue
            title = _clean_line(getattr(item, "title", "") or "")
            if title:
                titles.add(title.lower())

    try:
        walk(reader.outline)
    except Exception:
        return titles
    return titles


def _looks_like_heading(line: str, outline_titles: set[str]) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return False
    if stripped.lower() in outline_titles:
        return True
    if _HEADING_NAME_RE.match(stripped):
        return True
    if _NUMBERED_HEADING_RE.match(stripped) and not stripped.endswith((".", ",", ";", ":")):
        return True
    words = stripped.split()
    return (
        1 <= len(words) <= 8
        and stripped.isupper()
        and any(ch.isalpha() for ch in stripped)
        and not stripped.endswith((".", ","))
    )


def _detect_running_lines(pages: list[list[str]]) -> set[str]:
    """Identify repeated header/footer lines so body quotes stay uniquely anchorable."""
    if len(pages) < 3:
        return set()
    counts: dict[str, int] = {}
    for lines in pages:
        edge = [ln for ln in lines if ln][:2] + [ln for ln in lines if ln][-2:]
        normalized = {re.sub(r"\d+", "#", ln) for ln in edge if len(ln) <= 120}
        for key in normalized:
            counts[key] = counts.get(key, 0) + 1
    threshold = max(3, (len(pages) + 1) // 2)
    return {key for key, count in counts.items() if count >= threshold}


def extract_pdf(path: str) -> DocumentExtraction:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - locked core dependency
        raise DocumentExtractionError("pypdf is required for PDF extraction (uv sync --locked).") from exc
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise DocumentExtractionError("Encrypted PDF requires a password; refusing to extract.")
            except DocumentExtractionError:
                raise
            except Exception as exc:
                raise DocumentExtractionError(f"Encrypted PDF cannot be decrypted: {exc}") from exc
        page_objs = list(reader.pages)
    except PdfReadError as exc:
        raise DocumentExtractionError(f"Malformed PDF: {exc}") from exc
    if not page_objs:
        raise DocumentExtractionError("PDF contains no pages.")
    if len(page_objs) > MAX_PDF_PAGES:
        raise DocumentExtractionError(f"PDF exceeds {MAX_PDF_PAGES} pages.")

    outline_titles = _pdf_outline_titles(reader)
    raw_pages: list[list[str]] = []
    warnings: list[str] = []
    for index, page in enumerate(page_objs, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"page {index}: text extraction failed ({type(exc).__name__})")
            text = ""
        raw_pages.append([_clean_line(ln) for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")])

    running = _detect_running_lines(raw_pages)
    title = ""
    try:
        meta_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
        title = _clean_line(str(meta_title or ""))
    except Exception:
        title = ""

    page_blocks: list[str] = []
    for index, lines in enumerate(raw_pages, start=1):
        parts: list[str] = []
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                joined = " ".join(paragraph)
                joined = re.sub(r"(\w)- (\w)", r"\1\2", joined) if False else joined
                parts.append(joined)
                paragraph.clear()

        for line in lines:
            if not line:
                flush()
                continue
            if re.sub(r"\d+", "#", line) in running:
                continue
            if _looks_like_heading(line, outline_titles):
                flush()
                parts.append(f"### {line}")
                if not title and index == 1 and not _HEADING_NAME_RE.match(line):
                    title = line
                continue
            paragraph.append(line)
            if line.endswith((".", "?", "!", ":")) and len(" ".join(paragraph)) > 400:
                flush()
        flush()
        body = "\n\n".join(_neutralize(p) for p in parts) if parts else "_(no extractable text on this page)_"
        page_blocks.append(f"## [page {index}]\n\n{body}")

    header = f"# {_neutralize(title)}\n\n" if title else ""
    # Compute UTF-8 byte spans of every page block relative to the start of the body, then append
    # a trailing page index so the spans of earlier content never shift.
    body_text = header
    spans: list[dict[str, int]] = []
    for index, block in enumerate(page_blocks, start=1):
        if body_text and not body_text.endswith("\n\n"):
            body_text += "\n\n"
        start = len(body_text.encode("utf-8"))
        body_text += block
        end = len(body_text.encode("utf-8"))
        spans.append({"page": index, "start": start, "end": end})
    index_lines = "\n".join(f"- [page {s['page']}] span {s['start']}..{s['end']}" for s in spans)
    body_text += (
        "\n\n## Page Index\n\n"
        "UTF-8 byte spans are measured from the first byte of the extraction body (the line after the "
        "closing provenance frontmatter and origin comment).\n\n"
        f"{index_lines}\n"
    )
    markdown = body_text.replace("\r\n", "\n").rstrip() + "\n"
    if len(markdown) > MAX_EXTRACTED_CHARS:
        raise DocumentExtractionError(f"Extracted text exceeds {MAX_EXTRACTED_CHARS} characters.")
    try:
        import pypdf

        version = getattr(pypdf, "__version__", "unknown")
    except Exception:  # pragma: no cover
        version = "unknown"
    return DocumentExtraction(
        markdown=markdown,
        extractor="pypdf-text",
        model=f"pypdf {version}",
        model_digest="deterministic-no-model",
        limitations=(
            "Deterministic PDF text-layer extraction; scanned images are not OCRed, multi-column layout, "
            "tables, equations, and ligatures may be reordered or merged. Verify quotes against the archived PDF."
        ),
        title=title,
        page_count=len(page_objs),
        section_count=sum(block.count("\n### ") for block in page_blocks),
        page_spans=spans,
        warnings=warnings,
    )


# ─── DOCX ────────────────────────────────────────────────────────────────────

def _docx_paragraph_text(par: ET.Element) -> str:
    chunks: list[str] = []
    for node in par.iter():
        tag = node.tag
        if tag == f"{W_NS}t" and node.text:
            chunks.append(node.text)
        elif tag == f"{W_NS}tab":
            chunks.append(" ")
        elif tag in (f"{W_NS}br", f"{W_NS}cr"):
            chunks.append(" ")
    return _clean_line("".join(chunks))


def _docx_heading_level(par: ET.Element) -> int:
    ppr = par.find(f"{W_NS}pPr")
    if ppr is None:
        return 0
    style = ppr.find(f"{W_NS}pStyle")
    value = (style.get(f"{W_NS}val") if style is not None else "") or ""
    lowered = value.lower().replace(" ", "")
    if lowered == "title":
        return 1
    match = re.match(r"heading(\d)", lowered)
    if match:
        return max(1, min(6, int(match.group(1))))
    outline = ppr.find(f"{W_NS}outlineLvl")
    if outline is not None:
        try:
            return max(1, min(6, int(outline.get(f"{W_NS}val") or "0") + 1))
        except ValueError:
            return 0
    return 0


def extract_docx(path: str) -> DocumentExtraction:
    with _open_safe_zip(path) as archive:
        try:
            data = archive.read("word/document.xml")
        except KeyError as exc:
            raise DocumentExtractionError("DOCX is missing word/document.xml.") from exc
    root = _safe_xml(data)
    body = root.find(f"{W_NS}body")
    if body is None:
        raise DocumentExtractionError("DOCX document body is missing.")
    blocks: list[str] = []
    title = ""
    sections = 0
    tables = 0
    for child in body:
        if child.tag == f"{W_NS}p":
            text = _docx_paragraph_text(child)
            if not text:
                continue
            level = _docx_heading_level(child)
            if level:
                sections += 1
                if not title:
                    title = text
                blocks.append(f"{'#' * min(6, level + 1)} {_neutralize(text)}")
            else:
                blocks.append(_neutralize(text))
        elif child.tag == f"{W_NS}tbl":
            tables += 1
            rows: list[list[str]] = []
            for tr in child.iter(f"{W_NS}tr"):
                cells = [
                    " ".join(_docx_paragraph_text(p) for p in tc.iter(f"{W_NS}p")).strip()
                    for tc in tr.findall(f"{W_NS}tc")
                ]
                if any(cells):
                    rows.append(cells)
                if len(rows) >= MAX_TABLE_ROWS:
                    break
            if rows:
                lines = [f"#### [table {tables}]"]
                for r_index, cells in enumerate(rows, start=1):
                    lines.append(f"- [table {tables} row {r_index}] " + " | ".join(_neutralize(c) for c in cells))
                blocks.append("\n".join(lines))
    if not blocks:
        raise DocumentExtractionError("DOCX contains no extractable text.")
    header = f"# {_neutralize(title)}\n\n" if title and not blocks[0].lstrip("#").strip() == title else ""
    return DocumentExtraction(
        markdown=_finalize(header + "\n\n".join(blocks)),
        extractor="ooxml-docx",
        model="stdlib zipfile+xml.etree",
        model_digest="deterministic-no-model",
        limitations=(
            "Deterministic OOXML text extraction; comments, tracked changes, footnotes, embedded images, "
            "and text boxes are omitted. Verify quotes against the archived DOCX."
        ),
        title=title,
        section_count=sections,
    )


# ─── HTML / EPUB ─────────────────────────────────────────────────────────────

_HTML_BLOCKS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "tr", "dt", "dd", "figcaption")


def _html_to_blocks(markup: str | bytes, *, heading_offset: int = 0) -> tuple[str, list[str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    for bad in soup(["script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "head"]):
        if bad.name == "head":
            title_tag = bad.find("title")
            if title_tag is not None:
                bad.insert_before(title_tag.extract())
        bad.decompose()
    title_tag = soup.find("title")
    title = _clean_line(title_tag.get_text(" ")) if title_tag is not None else ""
    if title_tag is not None:
        title_tag.decompose()
    blocks: list[str] = []
    root = soup.body or soup
    for element in root.find_all(_HTML_BLOCKS):
        if any(parent.name in _HTML_BLOCKS for parent in element.parents if parent is not element):
            if element.name != "tr":
                continue
        name = element.name
        if name == "tr":
            cells = [_clean_line(c.get_text(" ")) for c in element.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if cells:
                blocks.append("- " + " | ".join(_neutralize(c) for c in cells))
            continue
        text = _clean_line(element.get_text(" "))
        if not text:
            continue
        if name.startswith("h") and len(name) == 2 and name[1].isdigit():
            level = min(6, int(name[1]) + heading_offset)
            blocks.append(f"{'#' * level} {_neutralize(text)}")
        elif name == "li":
            blocks.append(f"- {_neutralize(text)}")
        elif name == "blockquote":
            blocks.append(f"> {_neutralize(text)}")
        else:
            blocks.append(_neutralize(text))
    if not blocks:
        fallback = _clean_line(root.get_text(" "))
        if fallback:
            blocks.append(_neutralize(fallback))
    return title, blocks


def extract_html(path: str) -> DocumentExtraction:
    data = Path(path).read_bytes()
    title, blocks = _html_to_blocks(data, heading_offset=1)
    if not blocks:
        raise DocumentExtractionError("HTML contains no extractable text.")
    header = f"# {_neutralize(title)}\n\n" if title else ""
    sections = sum(1 for b in blocks if b.startswith("#"))
    return DocumentExtraction(
        markdown=_finalize(header + "\n\n".join(blocks)),
        extractor="bs4-html-local",
        model="beautifulsoup4 html.parser",
        model_digest="deterministic-no-model",
        limitations=(
            "Strictly local HTML parsing with no network fetches; scripts, styles, images, and remote "
            "resources are ignored. Verify quotes against the archived HTML file."
        ),
        title=title,
        section_count=sections,
    )


def extract_epub(path: str) -> DocumentExtraction:
    with _open_safe_zip(path) as archive:
        try:
            container = _safe_xml(archive.read("META-INF/container.xml"))
        except KeyError as exc:
            raise DocumentExtractionError("EPUB is missing META-INF/container.xml.") from exc
        rootfile = None
        for node in container.iter():
            if node.tag.endswith("rootfile") and node.get("full-path"):
                rootfile = node.get("full-path")
                break
        if not rootfile:
            raise DocumentExtractionError("EPUB container does not declare a rootfile.")
        opf_path = PurePosixPath(rootfile)
        if ".." in opf_path.parts or opf_path.is_absolute():
            raise DocumentExtractionError("EPUB rootfile path is unsafe.")
        try:
            opf = _safe_xml(archive.read(str(opf_path)))
        except KeyError as exc:
            raise DocumentExtractionError("EPUB package document is missing.") from exc
        manifest: dict[str, str] = {}
        book_title = ""
        for node in opf.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "item" and node.get("id") and node.get("href"):
                manifest[node.get("id")] = node.get("href")
            elif tag == "title" and not book_title and node.text:
                book_title = _clean_line(node.text)
        spine = [node.get("idref") for node in opf.iter() if node.tag.rsplit("}", 1)[-1] == "itemref" and node.get("idref")]
        base = opf_path.parent
        chapters: list[str] = []
        for chapter_index, idref in enumerate(spine, start=1):
            href = manifest.get(idref or "")
            if not href:
                continue
            member = (base / href.split("#", 1)[0]).as_posix()
            if ".." in PurePosixPath(member).parts:
                continue
            try:
                markup = archive.read(member)
            except KeyError:
                continue
            title, blocks = _html_to_blocks(markup, heading_offset=2)
            if not blocks:
                continue
            first_heading = next((b.lstrip("#").strip() for b in blocks if b.startswith("#")), "")
            label = first_heading or title or f"Section {chapter_index}"
            chapters.append(f"## Chapter {len(chapters) + 1}: {_neutralize(label)}\n\n" + "\n\n".join(blocks))
    if not chapters:
        raise DocumentExtractionError("EPUB contains no extractable chapters.")
    header = f"# {_neutralize(book_title)}\n\n" if book_title else ""
    return DocumentExtraction(
        markdown=_finalize(header + "\n\n".join(chapters)),
        extractor="ocf-epub",
        model="stdlib zipfile+xml.etree+beautifulsoup4",
        model_digest="deterministic-no-model",
        limitations=(
            "Deterministic EPUB spine extraction; images, footnote popups, CSS layout, and DRM-protected "
            "content are not extracted. Verify quotes against the archived EPUB."
        ),
        title=book_title,
        section_count=len(chapters),
    )


# ─── CSV / TSV ───────────────────────────────────────────────────────────────

def extract_delimited(path: str) -> DocumentExtraction:
    ext = os.path.splitext(path)[1].lower()
    delimiter = "\t" if ext == ".tsv" else ","
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentExtractionError("CSV/TSV must be UTF-8 encoded.") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = []
    for row in reader:
        if len(rows) > MAX_TABLE_ROWS:
            break
        rows.append([_clean_line(cell) for cell in row])
    rows = [r for r in rows if any(r)]
    if not rows:
        raise DocumentExtractionError("CSV/TSV contains no rows.")
    header = rows[0]
    data_rows = rows[1:]
    truncated = len(data_rows) > MAX_TABLE_ROWS
    data_rows = data_rows[:MAX_TABLE_ROWS]
    columns = [h or f"column_{i + 1}" for i, h in enumerate(header)]
    lines = [
        f"# {_neutralize(Path(path).name)}",
        "",
        f"Columns ({len(columns)}): " + " | ".join(_neutralize(c) for c in columns),
        f"Data rows: {len(data_rows)}" + (" (truncated)" if truncated else ""),
        "",
        "## Records",
        "",
    ]
    for index, row in enumerate(data_rows, start=1):
        pairs = []
        for col_index, value in enumerate(row):
            name = columns[col_index] if col_index < len(columns) else f"column_{col_index + 1}"
            pairs.append(f"{name}: {value}")
        lines.append(f"- [row {index}] " + _neutralize(" | ".join(pairs)))
    return DocumentExtraction(
        markdown=_finalize("\n".join(lines)),
        extractor="stdlib-csv" if delimiter == "," else "stdlib-tsv",
        model="python csv",
        model_digest="deterministic-no-model",
        limitations=(
            "Deterministic delimited-text extraction; types, formulas, and formatting are not interpreted. "
            "Each record is prefixed with a unique [row N] anchor."
        ),
        title=Path(path).name,
        row_count=len(data_rows),
        warnings=["rows truncated"] if truncated else [],
    )


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def extract_document(path: str | Path, *, max_bytes: int = MAX_DOC_BINARY_BYTES) -> DocumentExtraction:
    """Extract one local document into deterministic Markdown with page/section/row anchors."""
    target = os.path.abspath(str(path))
    ext = os.path.splitext(target)[1].lower()
    if ext not in DOC_EXTRACTOR_EXTS:
        raise DocumentExtractionError(f"Unsupported document type: {ext}")
    validate_document_source(target, max_bytes=max_bytes)
    if ext == ".pdf":
        return extract_pdf(target)
    if ext == ".docx":
        return extract_docx(target)
    if ext == ".epub":
        return extract_epub(target)
    if ext in {".html", ".htm"}:
        return extract_html(target)
    return extract_delimited(target)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Print deterministic Markdown extracted from a local document.")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="Print provenance metadata as JSON instead of Markdown.")
    args = parser.parse_args(argv)
    try:
        result = extract_document(args.path)
    except (DocumentExtractionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({**result.provenance(), "page_spans": result.page_spans, "warnings": result.warnings}, indent=2))
    else:
        sys.stdout.write(result.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
