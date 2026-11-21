# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "ollama",
# ]
# ///

import os
import re
import datetime
import shutil
import argparse
import sys
import ollama

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    RAW_DIR,
    ATOMS_DIR,
    CHROMA_PATH as CHROMA_DB_PATH,
    HOUSE_RULES as HOUSE_RULES_PATH,
    GEN_MODEL,
    EMBED_MODEL,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    append_archive_manifest_entry,
    archive_dir,
    build_archive_manifest_entry,
    ensure_dirs,
    file_sha256,
    safe_vault_reference,
)
from hardware_profile import strip_reasoning_think_blocks
from ollama_utils import chat_with_fallback, embed_text, embedding_metadata
from memory_format import (
    MEMORY_BOUNDARY,
    capture_origin,
    extract_memory_documents,
    normalize_memory_document,
    render_memory_document,
    set_string_list_field,
    split_frontmatter,
)
from memory_review import section_text
from refinery_targets import intake_status, is_supported_refinery_target
from vector_store import get_vector_collection
from review_atom import review_lock
from atom_mutator import atomic_write_bytes, refresh_lexical_index

DEFAULT_MAX_CHUNK_CHARS = 5500
DEFAULT_CHUNK_OVERLAP_CHARS = 450
MAX_ATOMS_PER_DOCUMENT = 40
_INIT_ATOMS_DIR = ATOMS_DIR


def _split_oversized_span(
    text: str,
    span_start: int,
    span_end: int,
    section_header: str,
    max_chunk_chars: int,
) -> list[tuple[int, int, str, bool, bool]]:
    """Split a single paragraph span exceeding max_chunk_chars on line, sentence, or word boundaries."""
    out: list[tuple[int, int, str, bool, bool]] = []
    sub_text = text[span_start:span_end]

    # Step 1: line boundaries
    line_spans: list[tuple[int, int]] = []
    cursor = span_start
    for line in sub_text.splitlines(keepends=True):
        line_spans.append((cursor, cursor + len(line)))
        cursor += len(line)

    # Step 2: refine any single line that still exceeds max_chunk_chars on sentence then word boundaries
    atomic_ranges: list[tuple[int, int]] = []
    for l_start, l_end in line_spans:
        if l_end - l_start <= max_chunk_chars:
            atomic_ranges.append((l_start, l_end))
            continue
        line_slice = text[l_start:l_end]
        sent_ranges: list[tuple[int, int]] = []
        s_prev = 0
        for match in re.finditer(r"[.!?]+\s+", line_slice):
            sent_ranges.append((l_start + s_prev, l_start + match.end()))
            s_prev = match.end()
        if s_prev < len(line_slice):
            sent_ranges.append((l_start + s_prev, l_end))

        for s_start, s_end in sent_ranges:
            cur = s_start
            while s_end - cur > max_chunk_chars:
                limit = cur + max_chunk_chars
                ws = text.rfind(" ", cur + 1, limit)
                cut = (ws + 1) if ws > cur else limit
                atomic_ranges.append((cur, cut))
                cur = cut
            if cur < s_end:
                atomic_ranges.append((cur, s_end))

    # Pack refined ranges greedily up to max_chunk_chars
    if not atomic_ranges:
        return [(span_start, span_end, section_header, False, False)]
    grp_start, grp_end = atomic_ranges[0]
    for r_start, r_end in atomic_ranges[1:]:
        if r_end - grp_start <= max_chunk_chars:
            grp_end = r_end
        else:
            out.append((grp_start, grp_end, section_header, False, False))
            grp_start, grp_end = r_start, r_end
    out.append((grp_start, grp_end, section_header, False, False))
    return out


def chunk_raw_document(
    raw_content: str,
    *,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> list[dict[str, object]]:
    """ deterministically split long documents on section/page/paragraph boundaries while keeping exact raw slices."""
    text = raw_content if isinstance(raw_content, str) else str(raw_content or "")
    max_chars = int(max_chunk_chars)
    if max_chars <= 0:
        raise ValueError("max_chunk_chars must be a positive integer.")
    overlap = max(0, min(int(overlap_chars), max_chars // 2))

    if len(text) <= max_chars:
        return [
            {
                "index": 0,
                "total": 1,
                "section_header": "",
                "text": text,
                "char_start": 0,
                "char_end": len(text),
            }
        ]

    body_offset = 0
    prov_match = re.match(
        r"^(?:\ufeff)?---\r?\n(.*?)\r?\n---\r?\n"
        r"(?:<!--\s*second_brain_origin:[^\n]*-->\r?\n)?"
        r"(?:<!--\s*extraction_body\s*-->\r?\n)?",
        text,
        flags=re.DOTALL,
    )
    if prov_match and re.search(r'(?m)^\s*type\s*:\s*["\']?raw_extraction["\']?\s*$', prov_match.group(1)):
        if prov_match.end() < len(text):
            body_offset = prov_match.end()

    # Build contiguous paragraph/heading spans over text[body_offset:]
    raw_blocks: list[tuple[int, int, str, bool, bool]] = []
    current_page = ""
    current_section = ""
    pos = body_offset
    block_start: int | None = None
    block_header = ""
    block_is_heading = False
    block_is_section = False
    prev_was_blank = False

    for line in text[body_offset:].splitlines(keepends=True):
        line_start = pos
        line_end = pos + len(line)
        pos = line_end
        stripped = line.strip()
        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        page_match = re.match(r"^#{1,6}\s*(\[page\s+\d+\].*)$", stripped, flags=re.IGNORECASE) if heading_match else None

        if heading_match:
            if block_start is not None and line_start > block_start:
                raw_blocks.append((block_start, line_start, block_header, block_is_heading, block_is_section))
            if page_match:
                current_page = stripped
                current_section = ""
            else:
                current_section = stripped
            active_header = (
                f"{current_page} | {current_section}"
                if (current_page and current_section)
                else (current_page or current_section)
            )
            block_start = line_start
            block_header = active_header
            block_is_heading = True
            block_is_section = True
            prev_was_blank = False
            continue

        active_header = (
            f"{current_page} | {current_section}"
            if (current_page and current_section)
            else (current_page or current_section)
        )
        if block_start is None:
            block_start = line_start
            block_header = active_header
            block_is_heading = False
            block_is_section = False
        elif (prev_was_blank and stripped != "") or block_is_heading:
            raw_blocks.append((block_start, line_start, block_header, block_is_heading, block_is_section))
            block_start = line_start
            block_header = active_header
            block_is_heading = False
            block_is_section = False

        prev_was_blank = stripped == ""

    if block_start is not None and pos > block_start:
        raw_blocks.append((block_start, pos, block_header, block_is_heading, block_is_section))

    if not raw_blocks:
        return [
            {
                "index": 0,
                "total": 1,
                "section_header": "",
                "text": text[body_offset:],
                "char_start": body_offset,
                "char_end": len(text),
            }
        ]

    spans: list[tuple[int, int, str, bool, bool]] = []
    for b_start, b_end, b_header, b_is_heading, b_is_section in raw_blocks:
        if b_end - b_start <= max_chars:
            spans.append((b_start, b_end, b_header, b_is_heading, b_is_section))
        else:
            split_sub = _split_oversized_span(text, b_start, b_end, b_header, max_chars)
            if split_sub:
                s0 = split_sub[0]
                spans.append((s0[0], s0[1], s0[2], b_is_heading, b_is_section))
                spans.extend(split_sub[1:])

    # Precompute section end positions so we can preferentially break before a new section that won't fit
    section_ends = [len(text)] * len(spans)
    next_sec_pos = len(text)
    for idx in range(len(spans) - 1, -1, -1):
        section_ends[idx] = next_sec_pos
        if spans[idx][4]:
            next_sec_pos = spans[idx][0]

    raw_chunks: list[dict[str, object]] = []
    span_idx = 0
    prefix_start = spans[0][0]

    while span_idx < len(spans):
        chunk_start = prefix_start
        if spans[span_idx][1] - chunk_start > max_chars:
            chunk_start = spans[span_idx][0]

        j = span_idx
        while j < len(spans):
            s_start, s_end, _s_hdr, s_is_heading, s_is_section = spans[j]
            if j > span_idx:
                if s_end - chunk_start > max_chars:
                    break
                # Never strand a heading at the end of a chunk without its following paragraph
                if s_is_heading and (j + 1 < len(spans)) and (spans[j + 1][1] - chunk_start > max_chars):
                    break
                # Prefer breaking at a new section/page boundary when chunk is >=50% full and new section exceeds budget
                if (
                    s_is_section
                    and (s_start - chunk_start) >= (max_chars // 2)
                    and (section_ends[j] - chunk_start) > max_chars
                ):
                    break
            j += 1

        chunk_end = spans[j - 1][1]
        chunk_header = spans[span_idx][2]
        if not chunk_header:
            for k in range(span_idx, j):
                if spans[k][2]:
                    chunk_header = spans[k][2]
                    break

        raw_chunks.append(
            {
                "section_header": chunk_header,
                "text": text[chunk_start:chunk_end],
                "char_start": chunk_start,
                "char_end": chunk_end,
            }
        )

        if j >= len(spans):
            break

        if overlap <= 0:
            span_idx = j
            prefix_start = spans[j][0]
            continue

        back = j
        while back - 1 > span_idx and (chunk_end - spans[back - 1][0]) <= overlap:
            back -= 1

        if back < j:
            span_idx = back
            prefix_start = spans[back][0]
        else:
            span_idx = j
            tail_min = max(chunk_start + 1, chunk_end - overlap)
            if tail_min < chunk_end and (spans[j][1] - tail_min) <= max_chars:
                tail_slice = text[tail_min:chunk_end]
                nl_pos = tail_slice.find("\n")
                sent_match = re.search(r"[.!?]+\s+", tail_slice)
                ws_match = re.search(r"\s+", tail_slice)
                if nl_pos >= 0 and nl_pos + 1 < len(tail_slice):
                    cand_start = tail_min + nl_pos + 1
                elif sent_match and sent_match.end() < len(tail_slice):
                    cand_start = tail_min + sent_match.end()
                elif ws_match and ws_match.end() < len(tail_slice):
                    cand_start = tail_min + ws_match.end()
                else:
                    cand_start = spans[j][0]
                prefix_start = cand_start if (spans[j][1] - cand_start) <= max_chars else spans[j][0]
            else:
                prefix_start = spans[j][0]

    total = len(raw_chunks)
    for idx, item in enumerate(raw_chunks):
        item["index"] = idx
        item["total"] = total
    return raw_chunks


def get_house_rules():
    with open(HOUSE_RULES_PATH, "r", encoding="utf-8") as f:
        return f.read()

def init_chroma():
    os.makedirs(CHROMA_DB_PATH, exist_ok=True)
    return get_vector_collection(CHROMA_DB_PATH, name="atoms", create=True)

def embed_atom_content(collection, atom_file, content):
    embedding = embed_text(content, model=EMBED_MODEL)
    
    collection.upsert(
        documents=[content],
        embeddings=[embedding],
        metadatas=[embedding_metadata(atom_file, content, EMBED_MODEL, embedding)],
        ids=[atom_file]
    )


def verify_embedding_available():
    """Fail before creating or archiving atoms when the configured embedder is unhealthy."""
    embed_text("Second Brain refinery preflight.", model=EMBED_MODEL)


def _normalized_claim_key(memory_doc: str) -> str:
    parsed = split_frontmatter(memory_doc)
    body = "\n".join(parsed[1]) if parsed is not None else memory_doc
    claim = section_text(body, "Claim")
    return re.sub(r"\s+", " ", claim).strip().rstrip(".;:").casefold()


def draft_memory_atoms(
    raw_content,
    raw_filename,
    rules,
    allow_fallback=False,
    model=None,
    *,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
):
    print(f"[*] Drafting memory atoms from {raw_filename}...")
    if allow_fallback:
        raise ValueError("Fallback atom generation is prohibited; defer this capture until the model is healthy.")

    chunks = chunk_raw_document(
        raw_content,
        max_chunk_chars=max_chunk_chars,
        overlap_chars=overlap_chars,
    )
    multi_chunk = len(chunks) > 1
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    collected: list[str] = []
    seen_claims: set[str] = set()
    last_unparsed_output = ""

    for chunk in chunks:
        if len(collected) >= MAX_ATOMS_PER_DOCUMENT:
            break
        chunk_idx = int(chunk["index"])
        chunk_total = int(chunk["total"])
        section_hdr = str(chunk.get("section_header") or "").strip()
        chunk_text = str(chunk["text"])

        chunk_context_lines: list[str] = []
        if multi_chunk:
            chunk_context_lines.append(
                f"Document chunk {chunk_idx + 1} of {chunk_total} (chars {chunk['char_start']}..{chunk['char_end']})."
            )
            chunk_context_lines.append(
                "If this chunk contains ONLY boilerplate, table of contents, navigation, bibliography/references, "
                "or page index entries with NO standalone durable claim, return ONLY the single word NONE and zero fenced blocks."
            )
        if section_hdr:
            chunk_context_lines.append(
                f"Active section / page context: {section_hdr} "
                "(context label only — copy ## Evidence quotes strictly verbatim from RAW CAPTURE below)."
            )
        extra_instructions = ("\n" + "\n".join(chunk_context_lines)) if chunk_context_lines else ""

        prompt = f"""
{rules}

YOUR TASK:
Read the following raw capture. Identify EVERY distinct, standalone idea, event, procedure, or self/system fact.
Extract each one into its own atomic memory using the exact Markdown template provided in the rules.
Classify each memory with one of these memory_type values: semantic, episodic, procedural, self.
Every fenced memory document MUST include YAML frontmatter followed by these exact section headings:
## Claim
## Evidence
## Why It Matters
Under ## Evidence, include an exact verbatim quote copied character-for-character from RAW CAPTURE so byte-offset provenance anchoring can verify it without manual repair.
Output ONLY fenced Markdown memory documents. No introduction, no summary.
Do not add prose before, between, or after the fenced Markdown memory documents.
Do not add headings like "Memory 1" outside the fenced Markdown memory documents.
If you produce more than one memory, put this exact boundary line between fenced documents:
{MEMORY_BOUNDARY}
Use today's date: {today_str}
Use the source filename: {raw_filename}{extra_instructions}

RAW CAPTURE:
{chunk_text}
"""
        try:
            response = chat_with_fallback(messages=[{"role": "user", "content": prompt}], model=model)
            output = strip_reasoning_think_blocks(response["message"]["content"])
        except Exception as exc:
            raise RuntimeError(f"LLM drafting unavailable; capture deferred: {exc}") from exc

        stripped_out = (output or "").strip()
        if multi_chunk and stripped_out.upper() in {"NONE", "NO_CLAIMS", "EMPTY"}:
            print(f"  [*] Chunk {chunk_idx + 1}/{chunk_total}: boilerplate/no durable claims; skipping chunk.")
            continue

        chunk_memories = extract_memory_documents(
            output,
            source_filename=raw_filename,
            current_date=today_str,
        )
        if not chunk_memories:
            last_unparsed_output = output or ""
            if multi_chunk:
                print(f"  [*] Chunk {chunk_idx + 1}/{chunk_total}: 0 valid memories parsed; continuing.")
                continue
            break

        for mem in chunk_memories:
            claim_key = _normalized_claim_key(mem)
            if claim_key and claim_key in seen_claims:
                continue
            if claim_key:
                seen_claims.add(claim_key)
            collected.append(mem)
            if len(collected) >= MAX_ATOMS_PER_DOCUMENT:
                break

    if collected:
        return collected

    preview = re.sub(r"\s+", " ", last_unparsed_output).strip()[:1200]
    print(f"[!] Unparseable model output preview: {preview}")
    raise RuntimeError("Could not parse model memories; capture deferred.")


def draft_atom(raw_content, raw_filename, rules):
    """Backward-compatible helper that returns the first drafted memory."""
    return draft_memory_atoms(raw_content, raw_filename, rules)[0]

def run_smart_critic(drafted_atom, collection):
    print("[*] Running Vector Critic for contradictions...")
    # Get embedding for drafted atom
    embedding = embed_text(drafted_atom, model=EMBED_MODEL)
    
    # Search Chroma for top 3 similar atoms
    results = collection.query(
        query_embeddings=[embedding],
        n_results=3,
        where={"embedding_model": EMBED_MODEL},
    )
    
    if not results["documents"] or not results["documents"][0]:
        print("  -> No similar atoms found. Passing.")
        return drafted_atom

    similar_atoms_text = "\n\n---\n\n".join(results["documents"][0])
    
    critic_prompt = f"""
You are the Critic. Your job is to read a NEW drafted atom, and compare it against EXISTING atoms retrieved from the vault.
Does the NEW drafted atom contradict or conflict with any of the EXISTING atoms?

NEW DRAFTED ATOM:
{drafted_atom}

EXISTING ATOMS:
{similar_atoms_text}

If there is a clear contradiction, append a [FRICTION] block to the END of the NEW DRAFTED ATOM and return the entire updated atom.
The [FRICTION] block format:
## [FRICTION] Raised by review
> **Conflicts with:** [[existing-atom-filename]]
> **Nature of conflict:** [One sentence describing the conflict]
> **Flagged:** {datetime.datetime.now().strftime('%Y-%m-%d')}
> **Status:** Awaiting human judgment. Not auto-resolved.

If there is NO contradiction, return the NEW DRAFTED ATOM exactly as it was provided, unchanged.
OUTPUT ONLY the markdown content.
"""
    try:
        response = chat_with_fallback(messages=[{"role": "user", "content": critic_prompt}], model=None)
        output = response["message"]["content"]
    except Exception as exc:
        print(f"[!] LLM critic unavailable; keeping drafted atom unchanged ({exc})")
        return drafted_atom
    
    match = re.search(r'```(?:markdown|md)?\n(.*?)\n```', output, re.DOTALL)
    if match:
        return match.group(1).strip()
    return output.strip()

def read_raw_capture(filepath):
    """Read a raw capture or a Web Scout .intake pointer."""
    if os.path.lexists(filepath) and _is_reparse_or_link(filepath):
        raise PermissionError(f"Refusing to read raw capture via symlink or reparse point: {filepath}")
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if ext != ".intake":
        return content, filename

    if intake_status(content) != "approved":
        raise RuntimeError("Intake pointer is quarantined and requires explicit owner approval before refinement.")
    
    source_match = re.search(r"^source:\s*(.+)$", content, re.MULTILINE)
    if not source_match:
        raise FileNotFoundError(f"Intake pointer is missing a valid 'source:' field: {filename}")
    
    source_ref = source_match.group(1).strip()
    source_path = safe_vault_reference(source_ref, allowed_exts={".md"})
    if not source_path or _is_reparse_or_link(source_path):
        raise FileNotFoundError(f"Intake pointer target does not exist or is unsafe: {source_ref}")
    
    with open(source_path, "r", encoding="utf-8") as f:
        source_content = f.read()
    
    return source_content, os.path.basename(source_path)


def brain_relative_path(path: str) -> str:
    """Return a path relative to the vault root inferred from the canonical atom directory."""
    brain_root = os.path.dirname(os.path.abspath(ATOMS_DIR))
    absolute = os.path.abspath(path)
    try:
        if os.path.normcase(os.path.commonpath([brain_root, absolute])) == os.path.normcase(brain_root):
            return os.path.relpath(absolute, brain_root).replace("\\", "/")
    except ValueError:
        pass
    raise RuntimeError(f"Evidence source is outside the vault root: {path}")

def atom_filename_for(final_atom):
    default_stem = f"{datetime.datetime.now().strftime('%Y-%m%d-%H%M%S')}-atom"
    parsed = split_frontmatter(final_atom)
    search_block = "\n".join(parsed[0]) if parsed is not None else final_atom
    id_match = re.search(r"^id:\s*(.+)$", search_block, re.MULTILINE)
    if not id_match:
        return f"{default_stem}.md"
    raw_id = id_match.group(1).strip().strip("\"'")
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", raw_id).strip("-")[:96].strip("-")
    if not slug:
        slug = default_stem
    elif slug.lower() in _WINDOWS_RESERVED_STEMS:
        slug = f"atom-{slug}"
    return f"{slug}.md"

def unique_atom_path(filename):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(ATOMS_DIR, filename)
    counter = 2
    
    while os.path.exists(candidate):
        candidate = os.path.join(ATOMS_DIR, f"{base}-{counter}{ext}")
        counter += 1
    
    return candidate, os.path.basename(candidate)

def archive_processed_capture(filepath):
    if not os.path.exists(filepath):
        return None
    
    dest_dir = archive_dir()
    os.makedirs(dest_dir, exist_ok=True)
    
    filename = os.path.basename(filepath)
    dest_path = os.path.join(dest_dir, filename)
    stem, ext = os.path.splitext(filename)
    counter = 2
    while os.path.exists(dest_path):
        dest_path = os.path.join(dest_dir, f"{stem}-{counter}{ext}")
        counter += 1
    
    shutil.move(filepath, dest_path)
    return dest_path

def _process_single_file(
    filepath,
    collection=None,
    rules=None,
    sync_existing=True,
    archive=True,
    allow_fallback=False,
    run_critic=False,
    model=None,
):
    """Process one raw capture into one or more memories and update Chroma."""
    if os.path.abspath(ATOMS_DIR) == os.path.abspath(_INIT_ATOMS_DIR):
        ensure_dirs()
    
    filename = os.path.basename(filepath)
    if not is_supported_refinery_target(filepath):
        print(f"[*] Skipping unsupported raw file: {filename}")
        return []
    if os.path.splitext(filename)[1].lower() == ".intake":
        with open(filepath, "r", encoding="utf-8") as intake_file:
            if intake_status(intake_file.read()) != "approved":
                print(f"[*] Deferring quarantined external intake: {filename}")
                return []

    rules = rules or get_house_rules()
    if run_critic:
        raise RuntimeError(
            "Automatic vector criticism is disabled until a trusted index generation is explicitly selected."
        )
    if sync_existing:
        print("[*] Canonical atoms are not incrementally embedded; run the staged vector rebuild after review.")
    
    source_sha256 = file_sha256(filepath)
    intake_source_reference = None
    if os.path.splitext(filename)[1].lower() == ".intake":
        with open(filepath, "r", encoding="utf-8") as intake_file:
            source_match = re.search(r"^source:\s*(.+)$", intake_file.read(), re.MULTILINE)
        if source_match:
            source_path = safe_vault_reference(source_match.group(1).strip(), allowed_exts={".md"})
            if source_path:
                intake_source_reference = brain_relative_path(source_path)
    raw_content, source_filename = read_raw_capture(filepath)
    source_origin = "web_remote" if os.path.splitext(filename)[1].lower() == ".intake" else capture_origin(raw_content, filename)
    drafts = draft_memory_atoms(
        raw_content,
        source_filename,
        rules,
        allow_fallback=allow_fallback,
        model=model,
    )

    lock_path = os.path.join(os.path.dirname(ATOMS_DIR), ".sidecar", "writer.lock")
    with review_lock(lock_path):
        if _is_reparse_or_link(ATOMS_DIR):
            raise PermissionError(f"Refusing to write atoms into symlinked directory: {ATOMS_DIR}")
        if not os.path.isfile(filepath) or _is_reparse_or_link(filepath) or file_sha256(filepath) != source_sha256:
            raise RuntimeError(f"Capture changed or was already processed while drafting: {filename}")
        os.makedirs(ATOMS_DIR, exist_ok=True)
        archived_path = archive_processed_capture(filepath) if archive else None
        source_reference = intake_source_reference or brain_relative_path(archived_path or filepath)
        saved_files = []
        for draft in drafts:
            final_atom = normalize_memory_document(
                draft,
                source_filename=source_filename,
                current_date=datetime.datetime.now().strftime("%Y-%m-%d"),
                origin=source_origin,
            )
            parsed_atom = split_frontmatter(final_atom)
            if parsed_atom is None:
                raise RuntimeError(f"Generated atom lost valid frontmatter before save: {filename}")
            final_atom = render_memory_document(
                set_string_list_field(parsed_atom[0], "sources", [source_reference], "confidence"),
                parsed_atom[1],
            )

            out_path, out_filename = unique_atom_path(atom_filename_for(final_atom))
            atomic_write_bytes(out_path, final_atom.replace("\r\n", "\n").encode("utf-8"))

            saved_files.append(out_filename)
            print(f"[+] Saved new memory atom: {out_filename}")

        if archived_path:
            append_archive_manifest_entry(
                build_archive_manifest_entry(
                    original_path=filepath,
                    archived_path=archived_path,
                    source_filename=source_filename,
                    generated_atoms=saved_files,
                    origin=source_origin,
                    source_sha256=source_sha256,
                    archived_sha256=file_sha256(archived_path),
                )
            )
        if saved_files:
            refresh_lexical_index(ATOMS_DIR, os.path.dirname(ATOMS_DIR))
        print(f"[*] Processed {filename}: {len(saved_files)} memory atom(s) saved.")
        return saved_files


def process_single_file(
    filepath,
    collection=None,
    rules=None,
    sync_existing=True,
    archive=True,
    allow_fallback=False,
    run_critic=False,
    model=None,
):
    """Draft outside the writer lock; serialize only verified canonical writes."""
    return _process_single_file(
        filepath,
        collection=collection,
        rules=rules,
        sync_existing=sync_existing,
        archive=archive,
        allow_fallback=allow_fallback,
        run_critic=run_critic,
        model=model,
    )

def process_raw_files():
    ensure_dirs()
    if _is_reparse_or_link(RAW_DIR):
        raise PermissionError(f"Refusing to scan symlinked 0-raw directory: {RAW_DIR}")
    rules = get_house_rules()

    raw_files = []
    for f in sorted(os.listdir(RAW_DIR)):
        if f == "reading-list.md" or f.startswith((".", "~")):
            continue
        candidate_path = os.path.join(RAW_DIR, f)
        try:
            if (
                not _is_reparse_or_link(candidate_path)
                and os.path.isfile(candidate_path)
                and is_supported_refinery_target(candidate_path)
            ):
                raw_files.append(f)
        except Exception as exc:
            print(f"[!] Deferred {f}: {exc}")

    if not raw_files:
        print("[*] No raw files to process.")
        return

    for file in raw_files:
        filepath = os.path.join(RAW_DIR, file)
        try:
            process_single_file(filepath, rules=rules, sync_existing=False)
        except Exception as exc:
            print(f"[!] Deferred {file}: {exc}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Process Second Brain raw captures into memory atoms.")
    parser.add_argument(
        "--file",
        help="Process exactly one raw capture or .intake pointer instead of the whole inbox.",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Do not archive the processed source file. Useful for controlled validation runs.",
    )
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Deprecated compatibility flag; model-backed extraction is always required.",
    )
    parser.add_argument(
        "--skip-critic",
        action="store_true",
        help="Deprecated compatibility flag; automatic vector criticism is disabled.",
    )
    parser.add_argument(
        "--model",
        help="Preferred Ollama generation model for this run.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    print("=== OLLAMA REFINERY ENGINE ===")
    try:
        if args.file:
            target = os.path.abspath(args.file)
            if not os.path.exists(target):
                print(f"Error: file not found: {target}")
                return 1
            if not is_supported_refinery_target(target):
                print(f"Error: unsupported refinery target: {target}")
                return 1
            saved = process_single_file(
                target,
                archive=not args.keep_source,
                allow_fallback=False,
                run_critic=False,
                model=args.model,
            )
            print(f"=== SINGLE FILE COMPLETE: {len(saved)} MEMORY ATOM(S) ===")
        else:
            process_raw_files()
        print("=== NIGHT SHIFT COMPLETE ===")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
