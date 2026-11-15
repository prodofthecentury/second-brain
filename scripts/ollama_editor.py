# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
# ]
# ///

import argparse
import ast
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import atomic_write_bytes, review_lock
from config import ATOMS_DIR, BRAIN_DIR, RAW_DIR, THREADS_DIR, GEN_MODEL, _is_reparse_or_link, ensure_dirs, is_content_markdown
from ollama_utils import chat_with_fallback
from review_atom import approved_revision_is_current

DEFAULT_MAX_TOPICS = 12


STOPWORDS = {
    "about", "after", "also", "and", "are", "because", "been", "being",
    "between", "from", "have", "into", "that", "their", "there", "this",
    "through", "under", "with", "without", "would",
}


def slugify(text: str, fallback: str = "general-synthesis") -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug or fallback


def extract_markdown_block(text: str) -> str:
    match = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def frontmatter_value(content: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", content, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def frontmatter_list(content: str, key: str) -> list[str]:
    inline = frontmatter_value(content, key)
    if inline.startswith("[") and inline.endswith("]"):
        return [
            item.strip().strip("\"'")
            for item in inline[1:-1].split(",")
            if item.strip().strip("\"'")
        ]

    block = re.search(
        rf"^{re.escape(key)}:\s*\n((?:\s+-\s+.+\n?)+)",
        content,
        re.MULTILINE,
    )
    if not block:
        return []
    return [
        line.split("-", 1)[1].strip().strip("\"'")
        for line in block.group(1).splitlines()
        if "-" in line
    ]


def extract_section(content: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def first_meaningful_words(text: str, count: int = 2) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text.lower())
    chosen = [word for word in words if word not in STOPWORDS]
    if not chosen:
        return "General Synthesis"
    return " ".join(word.title() for word in chosen[:count])


def load_atoms() -> list[dict]:
    ensure_dirs()
    if _is_reparse_or_link(ATOMS_DIR):
        raise PermissionError(f"Refusing to read atoms from symlinked directory: {ATOMS_DIR}")
    atoms = []
    for filename in sorted(os.listdir(ATOMS_DIR)):
        if not is_content_markdown(filename):
            continue
        filepath = os.path.join(ATOMS_DIR, filename)
        if _is_reparse_or_link(filepath) or not os.path.isfile(filepath):
            continue
        decision_log = os.path.join(RAW_DIR, "archive", "decision-record.jsonl")
        if not approved_revision_is_current(
            filepath,
            decision_log_path=decision_log,
            brain_dir=BRAIN_DIR,
            atoms_dir=ATOMS_DIR,
        ):
            continue
        with open(filepath, "r", encoding="utf-8") as file:
            content = file.read()

        claim = extract_section(content, "Claim")
        atom_id = frontmatter_value(content, "id")
        tags = frontmatter_list(content, "tags")
        title = atom_id or first_meaningful_words(claim or filename, count=4)

        atoms.append({
            "filename": filename,
            "content": content,
            "claim": claim,
            "tags": tags,
            "title": title,
        })
    return atoms


def fallback_topic_name(atom: dict) -> str:
    for tag in atom["tags"]:
        cleaned = re.sub(r"[-_]+", " ", tag).strip()
        if cleaned:
            return cleaned.title()
    return first_meaningful_words(atom["claim"] or atom["title"] or atom["filename"])


def compact_topic_map(topic_map: dict[str, list[str]], max_topics: int = DEFAULT_MAX_TOPICS) -> dict[str, list[str]]:
    items = sorted(topic_map.items(), key=lambda item: (-len(item[1]), item[0].lower()))
    if len(items) <= max_topics:
        return dict(items)

    primary = dict(items[: max_topics - 1])
    overflow = []
    for _, filenames in items[max_topics - 1:]:
        overflow.extend(filenames)
    primary["General Synthesis"] = sorted(overflow)
    return primary


def fallback_group_atoms(atoms: list[dict]) -> dict[str, list[str]]:
    grouped = {}
    for atom in atoms:
        topic = fallback_topic_name(atom)
        grouped.setdefault(topic, []).append(atom["filename"])
    return compact_topic_map(grouped)


def parse_topic_mapping(raw_text: str, valid_filenames: set[str]) -> dict[str, list[str]]:
    text = extract_markdown_block(raw_text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError):
            return {}

    if not isinstance(parsed, dict):
        return {}

    cleaned = {}
    for topic, filenames in parsed.items():
        if not isinstance(topic, str) or not isinstance(filenames, list):
            continue
        safe_files = [
            filename
            for filename in filenames
            if isinstance(filename, str) and filename in valid_filenames
        ]
        if safe_files:
            cleaned[topic.strip() or "General Synthesis"] = safe_files
    return compact_topic_map(cleaned)


def group_atoms(atoms: list[dict]) -> dict[str, list[str]]:
    print("[*] Grouping atoms by topic...")
    catalog = "\n".join([
        f"- {atom['filename']}: {atom['claim'] or atom['title']}"
        for atom in atoms
    ])

    prompt = f"""
Group these atomic notes into 1 to {DEFAULT_MAX_TOPICS} coherent synthesis topics.
Return only a valid JSON object mapping a short topic name to an array of exact filenames.
Do not include prose, markdown, comments, or filenames that are not listed.

ATOMS:
{catalog}
"""

    try:
        response = chat_with_fallback(messages=[{"role": "user", "content": prompt}], model=GEN_MODEL)
        topics = parse_topic_mapping(response["message"]["content"], {a["filename"] for a in atoms})
        if topics:
            return topics
        print("  [!] Could not parse model topic groups; using deterministic fallback.")
    except Exception as exc:
        print(f"  [!] Model grouping unavailable: {exc}")

    return fallback_group_atoms(atoms)


def compute_cluster_sha256(topic_name: str, atoms_list: list[dict]) -> str:
    canonical_atoms = sorted(
        (
            str(atom.get("filename", "")),
            hashlib.sha256(
                str(atom.get("content", "")).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            ).hexdigest(),
        )
        for atom in atoms_list
    )
    payload = json.dumps(
        {"topic_slug": slugify(topic_name), "atoms": canonical_atoms},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _attach_cluster_sha256(markdown_doc: str, cluster_sha256: str) -> str:
    normalized = markdown_doc.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[0].strip() == "---":
        kept: list[str] = ["---"]
        closed = False
        for line in lines[1:]:
            if not closed and line.strip() == "---":
                kept.append(f"cluster_sha256: {cluster_sha256}")
                kept.append("---")
                closed = True
            elif not closed and line.strip().startswith("cluster_sha256:"):
                continue
            else:
                kept.append(line)
        if closed:
            return "\n".join(kept).rstrip() + "\n"
    return normalized.rstrip() + "\n"


def existing_synthesized_cluster_hashes(threads_dir: str | None = None) -> set[str]:
    target_dir = threads_dir or THREADS_DIR
    if not os.path.isdir(target_dir) or _is_reparse_or_link(target_dir):
        return set()
    hashes: set[str] = set()
    for entry in sorted(os.listdir(target_dir)):
        if not is_content_markdown(entry):
            continue
        path = os.path.join(target_dir, entry)
        if _is_reparse_or_link(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                val = frontmatter_value(handle.read(8192), "cluster_sha256")
            if len(val) == 64 and all(ch in "0123456789abcdef" for ch in val.lower()):
                hashes.add(val.lower())
        except OSError:
            continue
    return hashes


def render_fallback_thread(topic_name: str, atoms_list: list[dict], *, cluster_sha256: str | None = None) -> str:
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cluster_hash = cluster_sha256 or compute_cluster_sha256(topic_name, atoms_list)
    atom_links = "\n".join(f"- [[{atom['filename']}]]" for atom in atoms_list)
    synthesis_lines = []
    for atom in atoms_list:
        claim = atom["claim"] or atom["title"]
        synthesis_lines.append(f"- [[{atom['filename']}]]: {claim}")

    return (
        "---\n"
        f'topic: "{topic_name.replace(chr(34), chr(39))}"\n'
        "type: thread\n"
        "status: active\n"
        f"last-updated: {today}\n"
        f"cluster_sha256: {cluster_hash}\n"
        "---\n\n"
        f"# {topic_name}\n\n"
        f"> **Thread summary**: This thread gathers {len(atoms_list)} related atom(s) for review and synthesis.\n\n"
        "## Synthesis\n"
        f"{chr(10).join(synthesis_lines)}\n\n"
        "## Atom Index\n"
        f"{atom_links}\n\n"
        "## Open Questions\n"
        "- What stronger source would confirm or challenge this cluster?\n"
        "- Which atom should be linked to adjacent threads next?\n"
    )


def synthesize_topic(topic_name: str, atoms_list: list[dict], *, cluster_sha256: str | None = None) -> str:
    print(f"[*] Synthesizing thread for: {topic_name}")
    cluster_hash = cluster_sha256 or compute_cluster_sha256(topic_name, atoms_list)
    atoms_text = "\n\n---\n\n".join([
        f"File: {atom['filename']}\n{atom['content']}"
        for atom in atoms_list
    ])
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    prompt = f"""
You are the Editor. Weave these atomic notes into one coherent synthesis document.
Write a living article, not a bullet summary. Cite atoms with [[filename]] links.

TOPIC: {topic_name}

ATOMS:
{atoms_text}

Return only the full Markdown document using this structure:
---
topic: "{topic_name.replace('"', "'")}"
type: thread
status: active
last-updated: {today}
---

# {topic_name}

> **Thread summary**: [1-2 sentences]

## Synthesis
[Prose synthesis with atom wikilinks]

## Atom Index
[List all contributing atoms as [[filename]] links]

## Open Questions
- [1-2 questions]
"""

    try:
        response = chat_with_fallback(messages=[{"role": "user", "content": prompt}], model=GEN_MODEL)
        output = extract_markdown_block(response["message"]["content"])
        if output.startswith("---") and "## Synthesis" in output:
            return _attach_cluster_sha256(output, cluster_hash)
        print("  [!] Model thread output did not match template; using fallback thread.")
    except Exception as exc:
        print(f"  [!] Model synthesis unavailable: {exc}")

    return render_fallback_thread(topic_name, atoms_list, cluster_sha256=cluster_hash)


def write_thread(topic_name: str, content: str) -> str:
    ensure_dirs()
    if _is_reparse_or_link(THREADS_DIR):
        raise PermissionError(f"Refusing to write threads into symlinked directory: {THREADS_DIR}")
    os.makedirs(THREADS_DIR, exist_ok=True)
    slug = slugify(topic_name)
    kept = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(kept).rstrip() + "\n"
    brain_root = BRAIN_DIR or os.path.dirname(os.path.abspath(THREADS_DIR))
    lock_path = os.path.join(brain_root, ".sidecar", "writer.lock")
    with review_lock(lock_path):
        if _is_reparse_or_link(THREADS_DIR):
            raise PermissionError(f"Refusing to write threads into symlinked directory: {THREADS_DIR}")
        suffix = 1
        while True:
            filename = f"{slug if suffix == 1 else f'{slug}-{suffix}'}.md"
            out_path = os.path.join(THREADS_DIR, filename)
            if not os.path.lexists(out_path):
                atomic_write_bytes(out_path, normalized.encode("utf-8"))
                print(f"  -> Saved thread: {filename}")
                return filename
            suffix += 1


def run_editor() -> list[str]:
    atoms = load_atoms()
    if not atoms:
        print("No atoms to synthesize.")
        return []

    topics = group_atoms(atoms)
    seen_hashes = existing_synthesized_cluster_hashes(THREADS_DIR)
    saved = []
    for topic, filenames in topics.items():
        topic_atoms = [atom for atom in atoms if atom["filename"] in filenames]
        if not topic_atoms:
            continue
        cluster_hash = compute_cluster_sha256(topic, topic_atoms)
        if cluster_hash in seen_hashes:
            print(f"[*] Skipping already-synthesized cluster: {topic} ({cluster_hash[:12]})")
            continue
        saved.append(write_thread(topic, synthesize_topic(topic, topic_atoms, cluster_sha256=cluster_hash)))
        seen_hashes.add(cluster_hash)
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthesize approved Second Brain atoms into topic threads.")
    parser.parse_args(argv)
    print("=== OLLAMA EDITOR ENGINE ===")
    run_editor()
    print("=== MORNING SHIFT COMPLETE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
