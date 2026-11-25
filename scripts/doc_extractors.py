# /// script
# requires-python = ">=3.11,<3.14"
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

from collections.abc import Callable
import csv
from dataclasses import dataclass, field
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
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
MAX_OCR_IMAGE_PIXELS = 25_000_000
EXTRACTION_BODY_MARKER = "<!-- extraction_body -->"
_TABLE_BLOCK_SENTINEL = "\x00__SB_MD_TABLE__\x00"

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
    # Preserve raw LaTeX display/inline spans verbatim without collapsing spacing inside equations
    if "$" in text or "\\[" in text or "\\(" in text or "\\begin{" in text:
        from math_equivalence import _LATEX_SPAN_RE

        placeholders: list[str] = []

        def _stash(match: re.Match[str]) -> str:
            idx = len(placeholders)
            placeholders.append(match.group(0))
            return f" __LATEX_SPAN_{idx}__ "

        masked = _LATEX_SPAN_RE.sub(_stash, text)
        cleaned = _WS_RE.sub(" ", masked).strip()
        for idx, original in enumerate(placeholders):
            cleaned = cleaned.replace(f"__LATEX_SPAN_{idx}__", original)
        return cleaned
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
    upper_data = data.upper()
    if b"<!DOCTYPE" in upper_data or b"<!ENTITY" in upper_data:
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


def _collect_pdf_positioned_fragments(page) -> list[tuple[float, float, float, str]]:
    """Extract (x, y, font_size, cleaned_text) fragments from a pypdf PageObject."""
    fragments: list[tuple[float, float, float, str]] = []

    def _visitor(text: str, cm: Any, tm: Any, _font_dict: Any, font_size: Any) -> None:
        cleaned = _clean_line(text)
        if not cleaned:
            return
        try:
            x = float(cm[0]) * float(tm[4]) + float(cm[2]) * float(tm[5]) + float(cm[4])
            y = float(cm[1]) * float(tm[4]) + float(cm[3]) * float(tm[5]) + float(cm[5])
            fsize = float(font_size or 11.0) if font_size else 11.0
        except Exception:
            x, y, fsize = 72.0, 0.0, 11.0
        fragments.append((x, y, max(6.0, min(fsize, 72.0)), cleaned))

    page.extract_text(visitor_text=_visitor)
    return fragments


_LAYOUT_CELL_RE = re.compile(r"\S+(?: {1,2}\S+)*")


def _extract_pdf_layout_rows(
    page,
    page_width: float,
) -> list[tuple[float, list[tuple[float, float, str]]]]:
    """Extract top-to-bottom rows of ``(x_start, x_end, cell_text)`` using pypdf's layout engine."""
    layout_text = ""
    try:
        layout_text = page.extract_text(extraction_mode="layout") or ""
    except Exception:
        layout_text = ""

    raw_lines = [
        ln.rstrip()
        for ln in layout_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if ln.strip()
    ]
    if raw_lines:
        max_cols = max(max((len(ln) for ln in raw_lines), default=80), 40)
        pt_per_col = page_width / float(max_cols)
        rows: list[tuple[float, list[tuple[float, float, str]]]] = []
        total = len(raw_lines)
        for idx, ln in enumerate(raw_lines):
            ry = float(total - idx) * 14.0
            cells: list[tuple[float, float, str]] = []
            for m in _LAYOUT_CELL_RE.finditer(ln):
                cleaned = _clean_line(m.group(0))
                if cleaned:
                    cells.append((m.start() * pt_per_col, m.end() * pt_per_col, cleaned))
            if cells:
                rows.append((ry, cells))
        if rows:
            return rows

    try:
        fragments = _collect_pdf_positioned_fragments(page)
    except Exception:
        fragments = []
    return _group_fragments_into_rows(fragments)


def _group_fragments_into_rows(
    fragments: list[tuple[float, float, float, str]],
    *,
    y_tol: float = 3.5,
    cell_gap_pt: float = 26.0,
) -> list[tuple[float, list[tuple[float, float, str]]]]:
    """Group positioned fragments into top-to-bottom rows of (x_start, x_end, cell_text) cells."""
    if not fragments:
        return []
    # Sort descending by y, then ascending by x
    ordered = sorted(fragments, key=lambda item: (-round(item[1] / y_tol), item[0]))
    y_bands: list[list[tuple[float, float, float, str]]] = []
    for frag in ordered:
        if not y_bands or abs(frag[1] - y_bands[-1][0][1]) > y_tol:
            y_bands.append([frag])
        else:
            y_bands[-1].append(frag)

    rows: list[tuple[float, list[tuple[float, float, str]]]] = []
    for band in y_bands:
        band.sort(key=lambda f: f[0])
        row_y = sum(f[1] for f in band) / len(band)
        cells: list[tuple[float, float, str]] = []
        for x, _y, fsize, txt in band:
            est_w = max(4.0, len(txt) * fsize * 0.48)
            x_end = x + est_w
            if not cells:
                cells.append((x, x_end, txt))
            else:
                prev_x, prev_end, prev_txt = cells[-1]
                if (x - prev_end) < cell_gap_pt:
                    merged_txt = f"{prev_txt} {txt}".strip()
                    cells[-1] = (prev_x, max(prev_end, x_end), merged_txt)
                else:
                    cells.append((x, x_end, txt))
        if cells:
            rows.append((row_y, cells))
    rows.sort(key=lambda r: -r[0])
    return rows


def _format_markdown_pipe_table(table_rows: list[list[str]]) -> str:
    col_count = max((len(r) for r in table_rows), default=0)
    if col_count < 2:
        return "\n".join(" ".join(r) for r in table_rows)

    def _fmt_row(cells: list[str]) -> str:
        padded = [(_neutralize(c).replace("|", "\\|").strip()) for c in cells[:col_count]]
        while len(padded) < col_count:
            padded.append("")
        return "| " + " | ".join(padded) + " |"

    header = _fmt_row(table_rows[0])
    sep = "| " + " | ".join(["---"] * col_count) + " |"
    body = [_fmt_row(r) for r in table_rows[1:]]
    return "\n".join([header, sep, *body])


def _reconstruct_pdf_page_lines(
    page,
) -> tuple[list[str], bool]:
    """Reconstruct reading-order lines and Markdown pipe tables from a PDF page.

    Returns ``(ordered_lines_or_table_sentinels, used_advanced_layout)``.
    """
    try:
        page_width = float(page.mediabox.width)
    except Exception:
        page_width = 612.0
    if page_width <= 0:
        page_width = 612.0

    rows = _extract_pdf_layout_rows(page, page_width)
    if not rows:
        raw = page.extract_text() or ""
        return [_clean_line(ln) for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")], False

    midpoint = page_width * 0.50

    # Check for 2-column academic prose layout:
    # Distinct left-column cells (ending near or before midpoint) and right-column cells (starting near/after midpoint),
    # where cells carry prose phrases rather than short tabular values.
    two_col_rows = [
        r for r in rows
        if len(r[1]) == 2
        and r[1][0][0] < (midpoint - 16.0)
        and r[1][0][1] <= (midpoint + 30.0)
        and r[1][1][0] >= (midpoint - 12.0)
    ]
    all_two_col_lengths = [
        len(cell[2])
        for _ry, cells in two_col_rows
        for cell in cells
    ]
    avg_two_col_len = (sum(all_two_col_lengths) / len(all_two_col_lengths)) if all_two_col_lengths else 0.0
    right_only_rows = [
        r for r in rows
        if len(r[1]) == 1 and r[1][0][0] >= (midpoint - 12.0) and len(r[1][0][2]) >= 18
    ]
    is_two_column = (len(two_col_rows) >= 3 and avg_two_col_len >= 22.0) or (
        (len(two_col_rows) + len(right_only_rows)) >= 4 and avg_two_col_len >= 20.0
    )

    if is_two_column:
        first_col_y = max(
            [r[0] for r in two_col_rows] + [r[0] for r in right_only_rows],
            default=rows[0][0],
        )
        last_col_y = min(
            [r[0] for r in two_col_rows] + [r[0] for r in right_only_rows],
            default=rows[-1][0],
        )
        top_full: list[tuple[float, str]] = []
        left_col: list[tuple[float, str]] = []
        right_col: list[tuple[float, str]] = []
        bottom_full: list[tuple[float, str]] = []

        for ry, cells in rows:
            if len(cells) == 1:
                cx, cend, ctxt = cells[0]
                if cx < (midpoint - 12.0) and cend > (midpoint + 36.0):
                    if ry >= first_col_y - 2.0:
                        top_full.append((ry, ctxt))
                    elif ry <= last_col_y + 2.0:
                        bottom_full.append((ry, ctxt))
                    else:
                        left_col.append((ry, ctxt))
                elif cx >= (midpoint - 12.0):
                    right_col.append((ry, ctxt))
                elif ry > first_col_y + 4.0:
                    top_full.append((ry, ctxt))
                elif ry < last_col_y - 4.0:
                    bottom_full.append((ry, ctxt))
                else:
                    left_col.append((ry, ctxt))
            else:
                for cx, _cend, ctxt in cells:
                    if cx < (midpoint - 12.0):
                        left_col.append((ry, ctxt))
                    else:
                        right_col.append((ry, ctxt))

        top_full.sort(key=lambda item: -item[0])
        left_col.sort(key=lambda item: -item[0])
        right_col.sort(key=lambda item: -item[0])
        bottom_full.sort(key=lambda item: -item[0])

        ordered_lines: list[str] = [txt for _, txt in top_full]
        if left_col:
            if ordered_lines:
                ordered_lines.append("")
            ordered_lines.extend(txt for _, txt in left_col)
        if right_col:
            if ordered_lines:
                ordered_lines.append("")
            ordered_lines.extend(txt for _, txt in right_col)
        if bottom_full:
            if ordered_lines:
                ordered_lines.append("")
            ordered_lines.extend(txt for _, txt in bottom_full)
        return ordered_lines, True

    # Single-column page: check for aligned multi-column tabular grids (>= 2 consecutive rows, >= 2 columns)
    out_lines: list[str] = []
    used_table = False
    idx = 0
    while idx < len(rows):
        _ry, cells = rows[idx]
        if 2 <= len(cells) <= 10 and all(len(c[2]) <= 64 for c in cells):
            anchor_xs = [c[0] for c in cells]
            table_group: list[list[str]] = [[c[2] for c in cells]]
            j = idx + 1
            while j < len(rows):
                _nry, ncells = rows[j]
                if len(ncells) != len(anchor_xs) or any(len(c[2]) > 64 for c in ncells):
                    break
                if all(abs(ncells[col_i][0] - anchor_xs[col_i]) <= 38.0 for col_i in range(len(anchor_xs))):
                    table_group.append([c[2] for c in ncells])
                    j += 1
                else:
                    break
            if len(table_group) >= 2:
                used_table = True
                out_lines.append("")
                out_lines.append(_TABLE_BLOCK_SENTINEL + _format_markdown_pipe_table(table_group))
                out_lines.append("")
                idx = j
                continue
        out_lines.append(" ".join(c[2] for c in cells))
        idx += 1

    if not used_table:
        # Preserve pypdf's native line spacing on plain single-column pages for exact backwards compatibility
        raw = page.extract_text() or ""
        plain_lines = [_clean_line(ln) for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        if any(plain_lines):
            return plain_lines, False
    return out_lines, used_table


def _sanitize_pdf_image_to_png(raw_bytes: bytes) -> bytes | None:
    """Validate embedded PDF image bytes via Pillow (blocking decompression bombs) and return clean PNG."""
    if not raw_bytes or len(raw_bytes) > 20 * 1024 * 1024:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as img:
            w, h = img.size
            if w <= 0 or h <= 0 or (w * h) > MAX_OCR_IMAGE_PIXELS:
                return None
            rgb = img.convert("RGB")
            buf = io.BytesIO()
            rgb.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


def _extract_page_image_bytes(page, *, max_depth: int = 3) -> list[bytes]:
    """Collect embedded image byte blobs from a PDF page and nested Form XObjects."""
    blobs: list[bytes] = []
    try:
        for img_obj in getattr(page, "images", ()) or ():
            raw = getattr(img_obj, "data", None)
            if isinstance(raw, (bytes, bytearray)) and raw:
                blobs.append(bytes(raw))
    except Exception:
        pass

    visited_refs: set[object] = set()

    def _walk_resources(resources_obj: Any, depth: int) -> None:
        if depth > max_depth or resources_obj is None:
            return
        try:
            res = resources_obj.get_object() if hasattr(resources_obj, "get_object") else resources_obj
            if not isinstance(res, dict):
                return
            xobjs = res.get("/XObject")
            xobjs = xobjs.get_object() if hasattr(xobjs, "get_object") else xobjs
            if not isinstance(xobjs, dict):
                return
            for _name, val in xobjs.items():
                ref = getattr(val, "indirect_reference", None)
                if ref is not None:
                    if ref in visited_refs:
                        continue
                    visited_refs.add(ref)
                obj = val.get_object() if hasattr(val, "get_object") else val
                subtype = str(obj.get("/Subtype") or "") if hasattr(obj, "get") else ""
                if subtype == "/Image":
                    data_bytes = None
                    try:
                        data_bytes = obj.get_data()
                    except Exception:
                        pass
                    if isinstance(data_bytes, (bytes, bytearray)) and data_bytes:
                        raw_b = bytes(data_bytes)
                        if raw_b not in blobs:
                            blobs.append(raw_b)
                elif subtype == "/Form":
                    _walk_resources(obj.get("/Resources"), depth + 1)
        except Exception:
            return

    if not blobs:
        try:
            _walk_resources(page.get("/Resources"), 0)
        except Exception:
            pass
    return blobs


def _page_has_non_text_visual_content(page) -> bool:
    """Return True when a PDF page embeds images or non-trivial drawing operators."""
    if _extract_page_image_bytes(page, max_depth=2):
        return True
    try:
        contents = page.get_contents()
        if contents is not None:
            raw_stream = contents.get_data() if hasattr(contents, "get_data") else b""
            if isinstance(raw_stream, (bytes, bytearray)) and len(bytes(raw_stream).strip()) > 12:
                return True
    except Exception:
        pass
    return False


def _rasterize_pdf_page_to_png(pdf_path: str | Path, page_number: int) -> bytes | None:
    """Render a 1-indexed PDF page into a Pillow-validated PNG byte string, or return None."""
    target = str(pdf_path)
    if not os.path.isfile(target) or page_number < 1:
        return None

    # 1. pypdfium2 (fast pure wheel when installed)
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]

        doc = pdfium.PdfDocument(target)
        try:
            if page_number <= len(doc):
                page = doc[page_number - 1]
                pil_img = page.render(scale=150.0 / 72.0).to_pil()
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                return _sanitize_pdf_image_to_png(buf.getvalue())
        finally:
            doc.close()
    except Exception:
        pass

    # 2. PyMuPDF (fitz)
    try:
        import fitz  # type: ignore[import-not-found]

        doc = fitz.open(target)
        try:
            if page_number <= len(doc):
                pix = doc[page_number - 1].get_pixmap(dpi=150)
                return _sanitize_pdf_image_to_png(pix.tobytes("png"))
        finally:
            doc.close()
    except Exception:
        pass

    # 3. Poppler pdftoppm CLI
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm and not _is_reparse_or_link(pdftoppm):
        with tempfile.TemporaryDirectory(prefix=".sb-pdftoppm-") as tmpdir:
            out_prefix = os.path.join(tmpdir, "page")
            try:
                proc = subprocess.run(
                    [pdftoppm, "-png", "-r", "150", "-f", str(page_number), "-l", str(page_number), "-singlefile", target, out_prefix],
                    capture_output=True,
                    timeout=20.0,
                    check=False,
                )
                out_png = Path(f"{out_prefix}.png")
                if proc.returncode == 0 and out_png.is_file():
                    return _sanitize_pdf_image_to_png(out_png.read_bytes())
            except Exception:
                pass

    # 4. MuPDF mutool CLI
    mutool = shutil.which("mutool")
    if mutool and not _is_reparse_or_link(mutool):
        with tempfile.TemporaryDirectory(prefix=".sb-mutool-") as tmpdir:
            out_png = Path(tmpdir) / "page.png"
            try:
                proc = subprocess.run(
                    [mutool, "draw", "-F", "png", "-r", "150", "-o", str(out_png), target, str(page_number)],
                    capture_output=True,
                    timeout=20.0,
                    check=False,
                )
                if proc.returncode == 0 and out_png.is_file():
                    return _sanitize_pdf_image_to_png(out_png.read_bytes())
            except Exception:
                pass

    # 5. Windows built-in WinRT PDF renderer (Windows.Data.Pdf.PdfDocument)
    if sys.platform == "win32":
        with tempfile.TemporaryDirectory(prefix=".sb-winpdf-") as tmpdir:
            out_png = Path(tmpdir) / "page.png"
            ps_script = (
                "$ErrorActionPreference='Stop'; "
                "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
                "$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime]; "
                "$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]; "
                "$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]; "
                "$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethodDefinition })[0]; "
                "$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and -not $_.IsGenericMethodDefinition })[0]; "
                "function Await($op, $t) { $m = $asTask.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); $task.Wait(); return $task.Result }; "
                "function AwaitAction($op) { $task = $asTaskAction.Invoke($null, @($op)); $task.Wait() }; "
                "$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:SB_PDF_PATH)) ([Windows.Storage.StorageFile]); "
                "$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument]); "
                "$idx = [uint32]$env:SB_PDF_PAGE_ZERO; "
                "if ($idx -ge $doc.PageCount) { exit 2 }; "
                "$pg = $doc.GetPage($idx); "
                "$stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new(); "
                "AwaitAction ($pg.RenderToStreamAsync($stream)); "
                "$stream.Seek(0); "
                "$netStream = [System.IO.WindowsRuntimeStreamExtensions]::AsStream($stream); "
                "$fs = [System.IO.File]::Create($env:SB_PNG_PATH); "
                "$netStream.CopyTo($fs); $fs.Dispose(); $pg.Dispose();"
            )
            env = dict(os.environ)
            env["SB_PDF_PATH"] = os.path.abspath(target)
            env["SB_PNG_PATH"] = str(out_png)
            env["SB_PDF_PAGE_ZERO"] = str(page_number - 1)
            ps_exe = os.path.join(
                os.environ.get("SystemRoot") or r"C:\Windows",
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
            if os.path.isfile(ps_exe) and not _is_reparse_or_link(ps_exe):
                try:
                    proc = subprocess.run(
                        [ps_exe, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                        env=env,
                        capture_output=True,
                        timeout=15.0,
                        check=False,
                    )
                    if proc.returncode == 0 and out_png.is_file():
                        return _sanitize_pdf_image_to_png(out_png.read_bytes())
                except Exception:
                    pass

    # 6. macOS Quartz / sips fallback (page 1 when sips is present)
    if sys.platform == "darwin":
        sips = shutil.which("sips")
        if sips and page_number == 1 and not _is_reparse_or_link(sips):
            with tempfile.TemporaryDirectory(prefix=".sb-sips-") as tmpdir:
                out_png = Path(tmpdir) / "page.png"
                try:
                    proc = subprocess.run(
                        [sips, "-s", "format", "png", target, "--out", str(out_png)],
                        capture_output=True,
                        timeout=15.0,
                        check=False,
                    )
                    if proc.returncode == 0 and out_png.is_file():
                        return _sanitize_pdf_image_to_png(out_png.read_bytes())
                except Exception:
                    pass

    return None


def _run_local_ocr_on_png(
    png_bytes: bytes,
    page_number: int,
    *,
    ocr_fn: Callable[[bytes, int], str] | None = None,
    allow_vision_ocr: bool = False,
) -> tuple[str, str | None]:
    """Run local-only OCR on a sanitized PNG image. Returns (extracted_text, engine_label)."""
    if ocr_fn is not None:
        try:
            raw_ocr = ocr_fn(png_bytes, page_number)  # type: ignore[misc]
        except TypeError:
            raw_ocr = ocr_fn(png_bytes)  # type: ignore[call-arg]
        txt = str(raw_ocr or "").strip()
        return (txt, "custom-ocr") if txt else ("", None)

    env_tess = (os.environ.get("BRAIN_TESSERACT_CMD") or "").strip()
    tess_bin = env_tess if env_tess else shutil.which("tesseract")
    if tess_bin and os.path.isfile(tess_bin) and not _is_reparse_or_link(tess_bin):
        fd, tmp_png = tempfile.mkstemp(prefix=".sb-pdf-ocr-", suffix=".png")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png_bytes)
            proc = subprocess.run(
                [tess_bin, tmp_png, "stdout", "--psm", "6"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout and proc.stdout.strip():
                return proc.stdout.strip(), "tesseract-local"
        except Exception:
            pass
        finally:
            if os.path.exists(tmp_png):
                try:
                    os.remove(tmp_png)
                except OSError:
                    pass

    use_vision = allow_vision_ocr or (os.environ.get("BRAIN_PDF_VISION_OCR") or "").strip() == "1"
    if use_vision:
        try:
            import base64
            from config import VISION_MODEL
            from hardware_profile import call_ollama_chat_maybe_no_think, strip_reasoning_think_blocks
            from host_adapters import validate_model_identifier
            import ollama
            from ollama_utils import _resolve_local_ollama_rule_and_url

            _eff_rule, base_url = _resolve_local_ollama_rule_and_url()
            try:
                from ingest_multimodal import select_vision_model

                chosen_vision = select_vision_model() or VISION_MODEL
            except Exception:
                chosen_vision = VISION_MODEL
            safe_vision_model = validate_model_identifier(chosen_vision, provider="ollama")
            b64_img = base64.b64encode(png_bytes).decode("ascii")
            client = ollama.Client(host=base_url, timeout=480.0, follow_redirects=False, trust_env=False)
            resp = call_ollama_chat_maybe_no_think(
                client.chat,
                model=safe_vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Transcribe all visible text on this scanned document page verbatim. Output only the transcribed text.",
                        "images": [b64_img],
                    }
                ],
                options={"temperature": 0.1, "repeat_penalty": 1.2, "num_predict": 384},
            )
            raw_txt = str((resp.get("message") or {}).get("content") or "").strip()
            txt = (strip_reasoning_think_blocks(raw_txt) or raw_txt).strip()
            if txt:
                return txt, f"ollama-vision:{safe_vision_model}"
        except Exception:
            pass

    return "", None


MAX_OCR_IMAGES_PER_PDF = 32


def extract_pdf(
    path: str,
    *,
    enable_ocr: bool = True,
    allow_vision_ocr: bool = False,
    ocr_fn: Callable[[bytes, int], str] | None = None,
) -> DocumentExtraction:
    if _is_reparse_or_link(str(path)):
        raise PermissionError(f"Refusing to extract PDF through symlink or reparse point: {path}")
    path = os.path.abspath(str(path))
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - locked core dependency
        raise DocumentExtractionError("pypdf is required for PDF extraction (uv sync --locked).") from exc
    try:
        reader = PdfReader(path, strict=False)
        if getattr(reader, "is_encrypted", False):
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
    page_visuals: list[bool] = []
    warnings: list[str] = []
    any_layout_upgraded = False
    ocr_engines_used: list[str] = []
    ocr_images_processed = 0
    ocr_budget_warned = False

    for index, page in enumerate(page_objs, start=1):
        try:
            lines, used_adv = _reconstruct_pdf_page_lines(page)
            if used_adv:
                any_layout_upgraded = True
        except Exception as exc:
            warnings.append(f"page {index}: text extraction failed ({type(exc).__name__})")
            lines = []

        printable_chars = sum(
            len(ln.replace(_TABLE_BLOCK_SENTINEL, "").strip())
            for ln in lines
            if ln and ln.strip()
        )
        has_visual = _page_has_non_text_visual_content(page)
        page_visuals.append(has_visual)
        if enable_ocr and printable_chars < 20:
            raw_img_blobs = _extract_page_image_bytes(page, max_depth=3)
            ocr_lines: list[str] = []
            for img_bytes in raw_img_blobs[:4]:
                if ocr_images_processed >= MAX_OCR_IMAGES_PER_PDF:
                    if not ocr_budget_warned:
                        warnings.append(f"OCR image budget ({MAX_OCR_IMAGES_PER_PDF} images per PDF) reached; remaining sparse pages skipped.")
                        ocr_budget_warned = True
                    break
                clean_png = _sanitize_pdf_image_to_png(img_bytes)
                if clean_png is None:
                    continue
                ocr_images_processed += 1
                ocr_txt, eng = _run_local_ocr_on_png(
                    clean_png,
                    index,
                    ocr_fn=ocr_fn,
                    allow_vision_ocr=allow_vision_ocr,
                )
                if ocr_txt:
                    if eng and eng not in ocr_engines_used:
                        ocr_engines_used.append(eng)
                    for raw_ln in ocr_txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                        cl = _clean_line(raw_ln)
                        if cl:
                            ocr_lines.append(cl)
            # Full-page rasterization fallback for vector graphics or Form XObject scans without embedded image blobs
            if not ocr_lines and ocr_images_processed < MAX_OCR_IMAGES_PER_PDF:
                raster_png = _rasterize_pdf_page_to_png(path, index)
                if raster_png is not None:
                    ocr_images_processed += 1
                    ocr_txt, eng = _run_local_ocr_on_png(
                        raster_png,
                        index,
                        ocr_fn=ocr_fn,
                        allow_vision_ocr=allow_vision_ocr,
                    )
                    if ocr_txt:
                        if eng and eng not in ocr_engines_used:
                            ocr_engines_used.append(eng)
                        for raw_ln in ocr_txt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                            cl = _clean_line(raw_ln)
                            if cl:
                                ocr_lines.append(cl)
            if ocr_lines:
                lines = [ln for ln in lines if ln] + ocr_lines

        raw_pages.append(lines)

    running = _detect_running_lines(
        [[ln for ln in p if not ln.startswith(_TABLE_BLOCK_SENTINEL)] for p in raw_pages]
    )
    title = ""
    try:
        meta_title = (reader.metadata or {}).get("/Title") if reader.metadata else None
        title = _clean_line(str(meta_title or ""))
    except Exception:
        title = ""

    page_blocks: list[str] = []
    pages_with_text = 0
    for index, lines in enumerate(raw_pages, start=1):
        parts: list[str] = []
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                parts.append(_neutralize(" ".join(paragraph)))
                paragraph.clear()

        for line in lines:
            if not line:
                flush()
                continue
            if line.startswith(_TABLE_BLOCK_SENTINEL):
                flush()
                parts.append(line[len(_TABLE_BLOCK_SENTINEL) :])
                continue
            if re.sub(r"\d+", "#", line) in running:
                continue
            if _looks_like_heading(line, outline_titles):
                flush()
                parts.append(f"### {_neutralize(line)}")
                if not title and index == 1 and not _HEADING_NAME_RE.match(line):
                    title = line
                continue
            paragraph.append(line)
            if line.endswith((".", "?", "!", ":")) and len(" ".join(paragraph)) > 400:
                flush()
        flush()
        if parts:
            pages_with_text += 1
            body = "\n\n".join(parts)
        else:
            has_vis = page_visuals[index - 1]
            reason = (
                "contains non-text visual content" if has_vis else "yielded no text objects"
            )
            warnings.append(
                f"Page {index} {reason} and OCR was unavailable (install tesseract CLI or set BRAIN_PDF_VISION_OCR=1)."
            )
            body = f"[OCR DEFERRED: page {index} {reason}; install tesseract or set BRAIN_PDF_VISION_OCR=1]"
        page_blocks.append(f"## [page {index}]\n\n{body}")

    if pages_with_text == 0:
        raise DocumentExtractionError(
            f"No extractable text found in PDF ({len(page_objs)} page(s)): "
            "all pages appear to be scanned images or vector graphics and no OCR backend "
            "(tesseract CLI or BRAIN_PDF_VISION_OCR=1) produced text."
        )

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
        "UTF-8 byte spans are measured from the first byte after the extraction body marker line "
        f"(`{EXTRACTION_BODY_MARKER}`).\n\n"
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

    if ocr_engines_used:
        extractor_name = "pypdf-layout+ocr"
        model_label = f"pypdf {version} + {', '.join(ocr_engines_used)}"
        model_digest = "local-ocr"
        limitations_note = (
            "Coordinate-aware PDF layout/table reconstruction with Pillow-sanitized local OCR fallback on sparse pages. "
            "Verify OCR quotes against the archived PDF."
        )
    elif any_layout_upgraded:
        extractor_name = "pypdf-layout"
        model_label = f"pypdf {version}"
        model_digest = "deterministic-no-model"
        limitations_note = (
            "Deterministic coordinate-aware PDF multi-column & Markdown pipe-table extraction. "
            "Verify quotes against the archived PDF."
        )
    else:
        extractor_name = "pypdf-text"
        model_label = f"pypdf {version}"
        model_digest = "deterministic-no-model"
        limitations_note = (
            "Deterministic PDF text-layer extraction; multi-column layout, tables, and sparse-page local OCR "
            "activate automatically when detected. Verify quotes against the archived PDF."
        )

    return DocumentExtraction(
        markdown=markdown,
        extractor=extractor_name,
        model=model_label,
        model_digest=model_digest,
        limitations=limitations_note,
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
    try:
        from math_equivalence import preserve_latex_in_html

        markup = preserve_latex_in_html(markup)
    except Exception:
        pass

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
