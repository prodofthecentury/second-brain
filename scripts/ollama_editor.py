# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
# ]
# ///

import ast
import datetime
import json
import os
import re

from config import ATOMS_DIR, BRAIN_DIR, RAW_DIR, THREADS_DIR, GEN_MODEL, ensure_dirs, is_content_markdown
from ollama_utils import chat_with_fallback
from review_atom import approved_revision_is_current


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
    atoms = []
    for filename in sorted(os.listdir(ATOMS_DIR)):
        if not is_content_markdown(filename):
            continue
        filepath = os.path.join(ATOMS_DIR, filename)
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


def compact_topic_map(topic_map: dict[str, list[str]], max_topics: int = 3) -> dict[str, list[str]]:
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
Group these atomic notes into 1 to 3 coherent synthesis topics.
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


def render_fallback_thread(topic_name: str, atoms_list: list[dict]) -> str:
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    atom_links = "\n".join(f"- [[{atom['filename']}]]" for atom in atoms_list)
    synthesis_lines = []
    for atom in atoms_list:
        claim = atom["claim"] or atom["title"]
        synthesis_lines.append(f"- [[{atom['filename']}]]: {claim}")

    return f"""---
topic: "{topic_name.replace('"', "'")}"
type: thread
status: active
last-updated: {today}
---

# {topic_name}

> **Thread summary**: This thread gathers {len(atoms_list)} related atom(s) for review and synthesis.

## Synthesis
{os.linesep.join(synthesis_lines)}

## Atom Index
{atom_links}

## Open Questions
- What stronger source would confirm or challenge this cluster?
- Which atom should be linked to adjacent threads next?
"""


def synthesize_topic(topic_name: str, atoms_list: list[dict]) -> str:
    print(f"[*] Synthesizing thread for: {topic_name}")
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
            return output
        print("  [!] Model thread output did not match template; using fallback thread.")
    except Exception as exc:
        print(f"  [!] Model synthesis unavailable: {exc}")

    return render_fallback_thread(topic_name, atoms_list)


def write_thread(topic_name: str, content: str) -> str:
    ensure_dirs()
    slug = slugify(topic_name)
    suffix = 1
    while True:
        filename = f"{slug if suffix == 1 else f'{slug}-{suffix}'}.md"
        out_path = os.path.join(THREADS_DIR, filename)
        try:
            with open(out_path, "x", encoding="utf-8") as file:
                file.write(content.rstrip() + "\n")
            print(f"  -> Saved thread: {filename}")
            return filename
        except FileExistsError:
            suffix += 1


def run_editor() -> list[str]:
    atoms = load_atoms()
    if not atoms:
        print("No atoms to synthesize.")
        return []

    topics = group_atoms(atoms)
    saved = []
    for topic, filenames in topics.items():
        topic_atoms = [atom for atom in atoms if atom["filename"] in filenames]
        if not topic_atoms:
            continue
        saved.append(write_thread(topic, synthesize_topic(topic, topic_atoms)))
    return saved


if __name__ == "__main__":
    print("=== OLLAMA EDITOR ENGINE ===")
    run_editor()
    print("=== MORNING SHIFT COMPLETE ===")
