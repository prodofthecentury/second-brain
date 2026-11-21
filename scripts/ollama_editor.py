# /// script
# requires-python = ">=3.11,<3.14"
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

from contextlib import redirect_stdout
from typing import Any

from atom_mutator import atomic_write_bytes, review_lock
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    BRIEFINGS_DIR,
    EXPERIENCES_DIR,
    GEN_MODEL,
    RAW_DIR,
    THREADS_DIR,
    _is_reparse_or_link,
    ensure_dirs,
    is_content_markdown,
    safe_file_in_dir,
)
from memory_format import evaluate_temporal_validity, remove_frontmatter_fields, split_frontmatter
from memory_review import parse_frontmatter_values, parse_scalar_list
from notes_store import NotePaths, extract_action_items
from hardware_profile import strip_reasoning_think_blocks
from ollama_utils import chat_with_fallback
from recall import normalize_project_tag, project_tags_match
from review_atom import approved_revision_is_current

DEFAULT_MAX_TOPICS = 12

_INIT_BRAIN_DIR = BRAIN_DIR
_INIT_ATOMS_DIR = ATOMS_DIR
_INIT_RAW_DIR = RAW_DIR
_INIT_THREADS_DIR = THREADS_DIR
_INIT_EXPERIENCES_DIR = EXPERIENCES_DIR
_INIT_BRIEFINGS_DIR = BRIEFINGS_DIR


def _resolve_vault_dirs(
    *,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    raw_dir: str | None = None,
    threads_dir: str | None = None,
    experiences_dir: str | None = None,
    briefings_dir: str | None = None,
) -> dict[str, str]:
    """Resolve vault directories dynamically, honoring explicit args, monkeypatched globals, and BRAIN_VAULT_DIR."""
    if brain_dir is not None:
        base = os.path.abspath(brain_dir)
        return {
            "brain": base,
            "atoms": os.path.abspath(atoms_dir) if atoms_dir is not None else os.path.join(base, "2-atoms"),
            "raw": os.path.abspath(raw_dir) if raw_dir is not None else os.path.join(base, "0-raw"),
            "threads": os.path.abspath(threads_dir) if threads_dir is not None else os.path.join(base, "3-threads"),
            "experiences": os.path.abspath(experiences_dir) if experiences_dir is not None else os.path.join(base, "4-experiences"),
            "briefings": os.path.abspath(briefings_dir) if briefings_dir is not None else os.path.join(base, "briefings"),
        }

    env_vault = (os.environ.get("BRAIN_VAULT_DIR") or "").strip()
    if BRAIN_DIR != _INIT_BRAIN_DIR:
        base = os.path.abspath(BRAIN_DIR)
        brain_overridden = True
    elif env_vault and os.path.abspath(env_vault) != os.path.abspath(_INIT_BRAIN_DIR):
        base = os.path.abspath(env_vault)
        brain_overridden = True
    else:
        base = os.path.abspath(BRAIN_DIR)
        brain_overridden = False

    def _pick(explicit: str | None, current_global: str, init_global: str, rel_subpath: str) -> str:
        if explicit is not None:
            return os.path.abspath(explicit)
        if current_global != init_global:
            return os.path.abspath(current_global)
        if brain_overridden:
            return os.path.join(base, rel_subpath)
        return os.path.abspath(current_global)

    return {
        "brain": base,
        "atoms": _pick(atoms_dir, ATOMS_DIR, _INIT_ATOMS_DIR, "2-atoms"),
        "raw": _pick(raw_dir, RAW_DIR, _INIT_RAW_DIR, "0-raw"),
        "threads": _pick(threads_dir, THREADS_DIR, _INIT_THREADS_DIR, "3-threads"),
        "experiences": _pick(experiences_dir, EXPERIENCES_DIR, _INIT_EXPERIENCES_DIR, "4-experiences"),
        "briefings": _pick(briefings_dir, BRIEFINGS_DIR, _INIT_BRIEFINGS_DIR, "briefings"),
    }


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
    cleaned = strip_reasoning_think_blocks(text)
    match = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", cleaned, re.DOTALL)
    return match.group(1).strip() if match else cleaned.strip()


def frontmatter_value(content: str, key: str) -> str:
    parsed = split_frontmatter(content)
    if parsed is None:
        return ""
    raw = parse_frontmatter_values(parsed[0]).get(key, "")
    if isinstance(raw, list):
        return ", ".join(str(x) for x in raw)
    return str(raw or "")


def frontmatter_list(content: str, key: str) -> list[str]:
    parsed = split_frontmatter(content)
    if parsed is None:
        return []
    raw = parse_frontmatter_values(parsed[0]).get(key, [])
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return parse_scalar_list(str(raw or ""))


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


def _as_string_list(raw_val: object) -> list[str]:
    if isinstance(raw_val, str):
        return [s for s in parse_scalar_list(raw_val) if s]
    if isinstance(raw_val, (list, tuple)):
        return [str(x).strip().strip("\"'") for x in raw_val if str(x).strip().strip("\"'")]
    return []


def load_atoms(
    *,
    include_preferences: bool = False,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    raw_dir: str | None = None,
) -> list[dict]:
    dirs = _resolve_vault_dirs(brain_dir=brain_dir, atoms_dir=atoms_dir, raw_dir=raw_dir)
    eff_brain = dirs["brain"]
    eff_atoms = dirs["atoms"]
    eff_raw = dirs["raw"]

    if eff_brain == os.path.abspath(_INIT_BRAIN_DIR) and eff_atoms == os.path.abspath(_INIT_ATOMS_DIR):
        ensure_dirs()
    if not os.path.isdir(eff_atoms):
        return []
    if _is_reparse_or_link(eff_atoms):
        raise PermissionError(f"Refusing to read atoms from symlinked directory: {eff_atoms}")

    decision_log = os.path.join(eff_raw, "archive", "decision-record.jsonl")
    atoms: list[dict] = []
    for filename in sorted(os.listdir(eff_atoms)):
        if not is_content_markdown(filename):
            continue
        filepath = os.path.join(eff_atoms, filename)
        if _is_reparse_or_link(filepath) or not os.path.isfile(filepath):
            continue
        if not approved_revision_is_current(
            filepath,
            decision_log_path=decision_log,
            brain_dir=eff_brain,
            atoms_dir=eff_atoms,
        ):
            continue
        with open(filepath, "r", encoding="utf-8") as file:
            content = file.read()

        parsed = split_frontmatter(content)
        fm = parse_frontmatter_values(parsed[0]) if parsed is not None else {}

        superseded_by = _as_string_list(fm.get("superseded_by", []))
        review_status = str(fm.get("review_status", "") or "").strip().lower()
        claim_state = str(fm.get("claim_state", "") or "").strip().lower()
        if superseded_by or review_status == "merged" or claim_state == "superseded":
            continue
        tv_atom = evaluate_temporal_validity(
            str(fm.get("valid_from") or "").strip(),
            str(fm.get("valid_until") or "").strip(),
        )
        if not tv_atom["active"] or tv_atom["status"] != "active":
            continue

        epistemic_label = str(fm.get("epistemic_label", "claim") or "claim").strip().lower()
        epistemic_contract = str(fm.get("epistemic_contract", "") or "").strip()
        is_preference = (epistemic_label == "preference") or bool(epistemic_contract)
        if is_preference and not include_preferences:
            continue

        contexts = _as_string_list(fm.get("contexts", []))
        proj_tags = sorted(
            {
                normalize_project_tag(ctx)
                for ctx in contexts
                if ctx.lower().startswith("project:") and normalize_project_tag(ctx)
            }
        )
        project_scope_key: tuple[str, ...] = tuple(proj_tags) if proj_tags else ("unscoped",)

        tags = _as_string_list(fm.get("tags", [])) or frontmatter_list(content, "tags")
        claim = extract_section(content, "Claim")
        atom_id = str(fm.get("id", "") or frontmatter_value(content, "id")).strip()
        title = atom_id or first_meaningful_words(claim or filename, count=4)

        atoms.append({
            "filename": filename,
            "content": content,
            "claim": claim,
            "tags": tags,
            "contexts": contexts,
            "project_scopes": project_scope_key,
            "project_scope_key": project_scope_key,
            "is_preference": is_preference,
            "memory_type": str(fm.get("memory_type", "semantic") or "semantic"),
            "created": str(fm.get("created", "") or ""),
            "last_reviewed": str(fm.get("last-reviewed", "") or ""),
            "title": title,
        })
    return atoms


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


def group_atoms(atoms: list[dict], *, model: str | None = None) -> dict[str, list[str]] | None:
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
        response = chat_with_fallback(messages=[{"role": "user", "content": prompt}], model=model)
        topics = parse_topic_mapping(response["message"]["content"], {a["filename"] for a in atoms})
        if topics:
            return topics
        print("  [!] DEFERRED: Model topic grouping output was invalid; deferring cluster without writing stub threads.")
        return None
    except Exception as exc:
        print(f"  [!] DEFERRED: Model grouping unavailable ({exc}); deferring cluster until Ollama is reachable.")
        return None


def _normalize_scope_label(
    project_scope: str | tuple[str, ...] | list[str] | None,
    atoms_list: list[dict] | None = None,
) -> tuple[str, list[str]]:
    """Return (canonical_scope_label, project_context_tags)."""
    if isinstance(project_scope, (tuple, list)):
        proj_tags = sorted(
            {
                normalize_project_tag(str(s))
                for s in project_scope
                if str(s).strip() and str(s).strip().lower() != "unscoped" and normalize_project_tag(str(s))
            }
        )
        return (",".join(proj_tags) if proj_tags else "unscoped"), proj_tags

    if isinstance(project_scope, str) and project_scope.strip():
        raw = project_scope.strip()
        if raw.lower() == "unscoped":
            return "unscoped", []
        proj_tags = sorted(
            {
                normalize_project_tag(part)
                for part in raw.split(",")
                if part.strip() and part.strip().lower() != "unscoped" and normalize_project_tag(part)
            }
        )
        return (",".join(proj_tags) if proj_tags else "unscoped"), proj_tags

    if atoms_list:
        inferred: set[str] = set()
        for atom in atoms_list:
            for sc in atom.get("project_scopes") or ():
                if str(sc).strip() and str(sc).strip().lower() != "unscoped":
                    norm = normalize_project_tag(str(sc))
                    if norm:
                        inferred.add(norm)
        if inferred:
            sorted_tags = sorted(inferred)
            return ",".join(sorted_tags), sorted_tags

    return "unscoped", []


def compute_cluster_sha256(
    topic_or_atoms: str | list[dict],
    atoms_list: list[dict] | None = None,
    *,
    project_scope: str | tuple[str, ...] | list[str] | None = None,
) -> str:
    if isinstance(topic_or_atoms, list) and atoms_list is None:
        effective_atoms = topic_or_atoms
        topic_slug = ""
    else:
        topic_slug = slugify(str(topic_or_atoms or ""))
        effective_atoms = list(atoms_list or [])

    scope_label, _ = _normalize_scope_label(project_scope, effective_atoms)
    canonical_atoms = sorted(
        (
            str(atom.get("filename", "")),
            hashlib.sha256(
                str(atom.get("content", "")).replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            ).hexdigest(),
        )
        for atom in effective_atoms
    )
    payload_obj: dict[str, object] = {
        "project_scope": scope_label,
        "atoms": canonical_atoms,
    }
    if topic_slug:
        payload_obj["topic_slug"] = topic_slug
    payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finalize_thread_document(
    markdown_doc: str,
    *,
    cluster_sha256: str,
    project_scope: str,
    contexts: list[str],
) -> str:
    normalized = markdown_doc.replace("\r\n", "\n").replace("\r", "\n")
    parsed = split_frontmatter(normalized)
    if parsed is None:
        return normalized.rstrip() + "\n"
    fm_lines, body_lines = parsed
    cleaned_fm = remove_frontmatter_fields(
        fm_lines,
        {"status", "project_scope", "contexts", "cluster_sha256"},
    )
    cleaned_fm.extend([
        "status: active",
        f"project_scope: {json.dumps(project_scope, ensure_ascii=False)}",
        f"contexts: {json.dumps(contexts, ensure_ascii=False)}",
        f"cluster_sha256: {cluster_sha256}",
    ])
    body_str = "\n".join(body_lines).strip()
    doc = "---\n" + "\n".join(cleaned_fm).strip() + "\n---"
    if body_str:
        doc += "\n\n" + body_str
    return doc.rstrip() + "\n"


def existing_synthesized_cluster_hashes(threads_dir: str | None = None) -> set[str]:
    target_dir = threads_dir if threads_dir is not None else _resolve_vault_dirs()["threads"]
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
                head = handle.read(8192)
            status_val = frontmatter_value(head, "status").strip().lower()
            if status_val != "active":
                continue
            val = frontmatter_value(head, "cluster_sha256")
            if len(val) == 64 and all(ch in "0123456789abcdef" for ch in val.lower()):
                hashes.add(val.lower())
        except OSError:
            continue
    return hashes


def synthesize_topic(
    topic_name: str,
    atoms_list: list[dict],
    *,
    cluster_sha256: str | None = None,
    project_scope: str | tuple[str, ...] | list[str] | None = None,
    model: str | None = None,
) -> str | None:
    scope_label, scope_contexts = _normalize_scope_label(project_scope, atoms_list)
    print(f"[*] Synthesizing thread for: {topic_name} (scope={scope_label})")
    cluster_hash = cluster_sha256 or compute_cluster_sha256(topic_name, atoms_list, project_scope=scope_label)
    atoms_text = "\n\n---\n\n".join([
        f"File: {atom['filename']}\n{atom['content']}"
        for atom in atoms_list
    ])
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    prompt = f"""
You are the Editor. Weave these atomic notes into one coherent synthesis document.
Write a living article, not a bullet summary. Cite atoms with [[filename]] links.

TOPIC: {topic_name}
PROJECT SCOPE: {scope_label}

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
        response = chat_with_fallback(messages=[{"role": "user", "content": prompt}], model=model)
        output = extract_markdown_block(response["message"]["content"])
        if (
            output.startswith("---")
            and split_frontmatter(output) is not None
            and "## Synthesis" in output
            and extract_section(output, "Synthesis")
        ):
            return _finalize_thread_document(
                output,
                cluster_sha256=cluster_hash,
                project_scope=scope_label,
                contexts=scope_contexts,
            )
        print(f"  [!] DEFERRED thread '{topic_name}': Model output did not match required thread template.")
        return None
    except Exception as exc:
        print(f"  [!] DEFERRED thread '{topic_name}': Model synthesis unavailable ({exc}).")
        return None


def write_thread(
    topic_name: str,
    content: str,
    *,
    threads_dir: str | None = None,
    brain_dir: str | None = None,
) -> str:
    dirs = _resolve_vault_dirs(brain_dir=brain_dir, threads_dir=threads_dir)
    eff_threads = dirs["threads"]
    eff_brain = dirs["brain"]

    if eff_brain == os.path.abspath(_INIT_BRAIN_DIR) and eff_threads == os.path.abspath(_INIT_THREADS_DIR):
        ensure_dirs()
    if os.path.lexists(eff_threads) and _is_reparse_or_link(eff_threads):
        raise PermissionError(f"Refusing to write threads into symlinked directory: {eff_threads}")
    os.makedirs(eff_threads, exist_ok=True)
    slug = slugify(topic_name)
    kept = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(kept).rstrip() + "\n"
    lock_path = os.path.join(eff_brain, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with review_lock(lock_path):
        if _is_reparse_or_link(eff_threads):
            raise PermissionError(f"Refusing to write threads into symlinked directory: {eff_threads}")
        suffix = 1
        while True:
            filename = f"{slug if suffix == 1 else f'{slug}-{suffix}'}.md"
            out_path = os.path.join(eff_threads, filename)
            if not os.path.lexists(out_path):
                atomic_write_bytes(out_path, normalized.encode("utf-8"))
                print(f"  -> Saved thread: {filename}")
                return filename
            suffix += 1


def _partition_matches_requested_scope(scope_key: tuple[str, ...], requested_scope: str | None) -> bool:
    if not requested_scope or not requested_scope.strip():
        return True
    raw = requested_scope.strip()
    if raw.lower() == "unscoped":
        return scope_key == ("unscoped",)
    norm_req = normalize_project_tag(raw)
    if not norm_req:
        return True
    if scope_key == ("unscoped",):
        return False
    return all(bool(project_tags_match((sc,), (norm_req,))) for sc in scope_key)


def _load_active_threads_for_briefing(threads_dir: str, requested_scope: str | None) -> list[dict[str, Any]]:
    if not os.path.isdir(threads_dir) or _is_reparse_or_link(threads_dir):
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(os.listdir(threads_dir)):
        if not is_content_markdown(entry):
            continue
        fpath = safe_file_in_dir(threads_dir, entry, allowed_exts={".md"}, must_exist=True)
        if not fpath or _is_reparse_or_link(fpath) or not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        parsed = split_frontmatter(text)
        fm = parse_frontmatter_values(parsed[0]) if parsed is not None else {}
        status = str(fm.get("status", frontmatter_value(text, "status")) or "").strip().lower()
        if status != "active":
            continue
        ctx_list = _as_string_list(fm.get("contexts", []))
        raw_ps = str(fm.get("project_scope", "") or "").strip()
        proj_tags = sorted(
            {
                normalize_project_tag(c)
                for c in [
                    *ctx_list,
                    *([p.strip() for p in raw_ps.split(",")] if raw_ps and raw_ps.lower() != "unscoped" else []),
                ]
                if c and c.lower().startswith("project:") and normalize_project_tag(c)
            }
        )
        scope_key: tuple[str, ...] = tuple(proj_tags) if proj_tags else ("unscoped",)
        if not _partition_matches_requested_scope(scope_key, requested_scope):
            continue
        topic = str(fm.get("topic", "") or "").strip() or entry.removesuffix(".md")
        summary_match = re.search(r">\s*\*\*Thread summary\*\*:\s*(.+)$", text, re.MULTILINE)
        summary = summary_match.group(1).strip() if summary_match else extract_section(text, "Synthesis")[:240]
        out.append({
            "filename": entry,
            "topic": topic,
            "project_scopes": list(scope_key),
            "last_updated": str(fm.get("last-updated", "") or ""),
            "summary": re.sub(r"\s+", " ", summary).strip()[:280],
        })
    return out


def _load_recent_experiences_for_briefing(
    experiences_dir: str,
    cutoff_date: datetime.date,
    requested_scope: str | None,
) -> list[dict[str, Any]]:
    if not os.path.isdir(experiences_dir) or _is_reparse_or_link(experiences_dir):
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(os.listdir(experiences_dir)):
        if not is_content_markdown(entry):
            continue
        fpath = safe_file_in_dir(experiences_dir, entry, allowed_exts={".md"}, must_exist=True)
        if not fpath or _is_reparse_or_link(fpath) or not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        parsed = split_frontmatter(text)
        fm = parse_frontmatter_values(parsed[0]) if parsed is not None else {}
        raw_created = str(fm.get("created_at", "") or "").strip()
        exp_date: datetime.date | None = None
        if len(raw_created) >= 10:
            try:
                exp_date = datetime.date.fromisoformat(raw_created[:10])
            except ValueError:
                exp_date = None
        if exp_date is None:
            try:
                exp_date = datetime.date.fromtimestamp(os.path.getmtime(fpath))
            except OSError:
                continue
        if exp_date < cutoff_date:
            continue

        ctx_list = _as_string_list(fm.get("context", []))
        proj_tags = sorted(
            {
                normalize_project_tag(c)
                for c in ctx_list
                if c.lower().startswith("project:") and normalize_project_tag(c)
            }
        )
        scope_key: tuple[str, ...] = tuple(proj_tags) if proj_tags else ("unscoped",)
        if not _partition_matches_requested_scope(scope_key, requested_scope):
            continue

        goal = str(fm.get("goal", "") or extract_section(text, "Goal")).strip()
        success_str = str(fm.get("success", "true")).strip().lower()
        result_sec = extract_section(text, "Result")
        out.append({
            "filename": entry,
            "created_at": raw_created or exp_date.isoformat(),
            "goal": goal,
            "success": success_str == "true",
            "result": re.sub(r"\s+", " ", result_sec).strip()[:280],
        })
    return out


def synthesize_weekly_briefing(
    *,
    project_scope: str | None = None,
    days: int = 7,
    brain_dir: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Synthesize a weekly executive briefing into brain/briefings/ when Ollama is reachable; defer cleanly offline."""
    dirs = _resolve_vault_dirs(brain_dir=brain_dir)
    eff_brain = dirs["brain"]
    eff_atoms = dirs["atoms"]
    eff_raw = dirs["raw"]
    eff_threads = dirs["threads"]
    eff_experiences = dirs["experiences"]
    eff_briefings = dirs["briefings"]

    window_days = max(1, int(days))
    today_date = datetime.date.today()
    today_str = today_date.isoformat()
    cutoff_date = today_date - datetime.timedelta(days=window_days)

    raw_scope = (project_scope or "").strip()
    want_unscoped = raw_scope.lower() == "unscoped"
    norm_scope = normalize_project_tag(raw_scope) if (raw_scope and not want_unscoped) else None
    effective_scope_label = "unscoped" if want_unscoped else (norm_scope or "all")

    all_verified_atoms = load_atoms(
        include_preferences=True,
        brain_dir=eff_brain,
        atoms_dir=eff_atoms,
        raw_dir=eff_raw,
    )
    scoped_atoms = [
        atom
        for atom in all_verified_atoms
        if _partition_matches_requested_scope(tuple(atom["project_scopes"]), raw_scope or None)
    ]
    factual_atoms = [a for a in scoped_atoms if not a.get("is_preference")]
    preference_atoms = [a for a in scoped_atoms if a.get("is_preference")]

    active_threads = _load_active_threads_for_briefing(eff_threads, raw_scope or None)
    recent_experiences = _load_recent_experiences_for_briefing(eff_experiences, cutoff_date, raw_scope or None)

    note_paths = NotePaths.for_brain(eff_brain)
    action_items = extract_action_items(
        paths=note_paths,
        raw_dir=eff_raw,
        include_completed=False,
        include_raw=True,
        project=norm_scope,
    )
    if want_unscoped:
        action_items = [item for item in action_items if not item.get("projects")]

    counts = {
        "factual_atoms": len(factual_atoms),
        "preference_atoms": len(preference_atoms),
        "active_threads": len(active_threads),
        "recent_experiences": len(recent_experiences),
        "open_action_items": len(action_items),
    }

    factual_block = "\n".join(
        f"- [[{a['filename']}]] ({a['memory_type']}): {a['claim'] or a['title']}"
        for a in factual_atoms[:30]
    ) or "- None recorded."
    pref_block = "\n".join(
        f"- [[{p['filename']}]] (owner subjective preference): {p['claim'] or p['title']}"
        for p in preference_atoms[:15]
    ) or "- None recorded."
    threads_block = "\n".join(
        f"- [[{t['filename']}]] — {t['topic']}: {t['summary']}"
        for t in active_threads[:20]
    ) or "- None active."
    exp_block = "\n".join(
        f"- [[{e['filename']}]] ({'succeeded' if e['success'] else 'failed'}, {e['created_at'][:10]}): {e['goal']} — {e['result']}"
        for e in recent_experiences[:20]
    ) or "- None in window."
    tasks_block = "\n".join(
        f"- [ ] ({it['source_kind']}:{it['filename']}:{it['line_number']}) {it['text']}"
        + (f" [due={it['due_date']}]" if it.get("due_date") else "")
        + (f" [priority={it['priority']}]" if it.get("priority") else "")
        for it in action_items[:30]
    ) or "- No open action items."

    prompt = f"""
You are the Second Brain Weekly Briefing Editor.
Synthesize a concise, high-signal executive narrative covering the past {window_days} days (as of {today_str}, scope: {effective_scope_label}).

STRICT EPISTEMIC RULES:
1. Only items under VERIFIED FACTUAL & PROCEDURAL ATOMS and ACTIVE SYNTHESIS THREADS are owner-verified claims. Cite verified atoms using [[filename]] links.
2. NEVER mix OWNER SUBJECTIVE PREFERENCES into factual claims; treat preferences strictly as subjective owner guardrails.
3. Items under [OPEN ACTION ITEMS — UNVERIFIED WORKING NOTES] come from unverified working notes and raw captures; NEVER state them as verified facts.
4. Write clear Markdown narrative sections:
   ## Executive Summary
   ## Verified Knowledge & Thread Highlights
   ## Operational Episodes & Lessons

VERIFIED FACTUAL & PROCEDURAL ATOMS:
{factual_block}

OWNER SUBJECTIVE PREFERENCES (NON-FACTUAL GUARDRAILS):
{pref_block}

ACTIVE SYNTHESIS THREADS (3-threads/):
{threads_block}

RECENT EXPERIENCES (Last {window_days} days, 4-experiences/):
{exp_block}

[OPEN ACTION ITEMS — UNVERIFIED WORKING NOTES]:
{tasks_block}
"""

    try:
        response = chat_with_fallback(messages=[{"role": "user", "content": prompt}], model=model)
        raw_prose = extract_markdown_block(response["message"]["content"])
    except Exception as exc:
        print(f"[!] DEFERRED weekly briefing: Ollama unavailable ({exc}); no counterfeit briefing written.")
        return {
            "status": "deferred",
            "reason": "ollama_unavailable",
            "error": str(exc),
            "filename": None,
            "path": None,
            "project_scope": effective_scope_label,
            "window_days": window_days,
            "counts": counts,
        }

    parsed_prose = split_frontmatter(raw_prose)
    narrative_body = ("\n".join(parsed_prose[1]).strip() if parsed_prose is not None else raw_prose.strip())
    if not narrative_body:
        print("[!] DEFERRED weekly briefing: empty model synthesis output; no counterfeit briefing written.")
        return {
            "status": "deferred",
            "reason": "empty_model_output",
            "filename": None,
            "path": None,
            "project_scope": effective_scope_label,
            "window_days": window_days,
            "counts": counts,
        }

    sections: list[str] = [
        "---",
        "type: briefing",
        "briefing_kind: weekly",
        "status: active",
        f"created: {today_str}",
        f"window_days: {window_days}",
        f"project_scope: {json.dumps(effective_scope_label, ensure_ascii=False)}",
        f"verified_atom_count: {counts['factual_atoms']}",
        f"preference_atom_count: {counts['preference_atoms']}",
        f"active_thread_count: {counts['active_threads']}",
        f"recent_experience_count: {counts['recent_experiences']}",
        f"open_action_item_count: {counts['open_action_items']}",
        "---",
        "",
        f"# Weekly Briefing — {today_str}",
        "",
        narrative_body,
    ]

    if preference_atoms:
        sections.extend([
            "",
            "## Owner Preferences (Subjective — Separate Epistemic Tier)",
            *(f"- [[{p['filename']}]]: {p['claim'] or p['title']}" for p in preference_atoms[:20]),
        ])

    if "[OPEN ACTION ITEMS — UNVERIFIED WORKING NOTES]" not in narrative_body:
        sections.extend([
            "",
            "## [OPEN ACTION ITEMS — UNVERIFIED WORKING NOTES]",
            tasks_block,
        ])

    rendered_bytes = ("\n".join(sections).rstrip() + "\n").encode("utf-8")

    if os.path.lexists(eff_briefings) and _is_reparse_or_link(eff_briefings):
        raise PermissionError(f"Refusing to write weekly briefing into symlinked directory: {eff_briefings}")
    os.makedirs(eff_briefings, exist_ok=True)
    lock_path = os.path.join(eff_brain, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    base_stem = f"{today_str}-weekly-briefing"
    with review_lock(lock_path):
        if _is_reparse_or_link(eff_briefings):
            raise PermissionError(f"Refusing to write weekly briefing into symlinked directory: {eff_briefings}")
        suffix = 1
        while True:
            cand_name = f"{base_stem}.md" if suffix == 1 else f"{base_stem}-{suffix}.md"
            out_path = safe_file_in_dir(eff_briefings, cand_name, allowed_exts={".md"}, must_exist=False)
            if not out_path:
                raise ValueError(f"Unsafe briefing filename: {cand_name!r}")
            if not os.path.lexists(out_path):
                atomic_write_bytes(out_path, rendered_bytes)
                print(f"[+] Saved weekly briefing: {cand_name}")
                return {
                    "status": "saved",
                    "filename": cand_name,
                    "path": out_path,
                    "project_scope": effective_scope_label,
                    "window_days": window_days,
                    "counts": counts,
                }
            suffix += 1


def run_editor(
    *,
    include_briefing: bool = True,
    threads_only: bool = False,
    briefing_only: bool = False,
    project_scope: str | None = None,
    days: int = 7,
    brain_dir: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    dirs = _resolve_vault_dirs(brain_dir=brain_dir)
    do_threads = not briefing_only
    do_briefing = False if threads_only else (True if briefing_only else bool(include_briefing))

    saved_threads: list[str] = []
    deferred_clusters = 0
    skipped_clusters = 0
    partition_labels: list[str] = []

    if do_threads:
        atoms = load_atoms(include_preferences=False, brain_dir=dirs["brain"], atoms_dir=dirs["atoms"], raw_dir=dirs["raw"])
        partitions: dict[tuple[str, ...], list[dict]] = {}
        for atom in atoms:
            scope_key = tuple(atom.get("project_scopes") or ("unscoped",))
            if not _partition_matches_requested_scope(scope_key, project_scope):
                continue
            partitions.setdefault(scope_key, []).append(atom)

        if not partitions:
            print("No eligible non-preference, non-superseded approved atoms to synthesize into threads.")
        else:
            seen_hashes = existing_synthesized_cluster_hashes(dirs["threads"])
            for scope_key in sorted(partitions.keys()):
                scoped_atoms = partitions[scope_key]
                scope_label, _ = _normalize_scope_label(scope_key, scoped_atoms)
                partition_labels.append(scope_label)
                print(f"[*] Processing scope partition '{scope_label}' ({len(scoped_atoms)} atom(s))...")
                topics = group_atoms(scoped_atoms, model=model)
                if topics is None:
                    deferred_clusters += 1
                    continue
                for topic, filenames in topics.items():
                    topic_atoms = [atom for atom in scoped_atoms if atom["filename"] in filenames]
                    if not topic_atoms:
                        continue
                    cluster_hash = compute_cluster_sha256(topic, topic_atoms, project_scope=scope_key)
                    if cluster_hash in seen_hashes:
                        skipped_clusters += 1
                        print(f"[*] Skipping already-synthesized cluster: {topic} [{scope_label}] ({cluster_hash[:12]})")
                        continue
                    synthesized = synthesize_topic(
                        topic,
                        topic_atoms,
                        cluster_sha256=cluster_hash,
                        project_scope=scope_key,
                        model=model,
                    )
                    if synthesized is None:
                        deferred_clusters += 1
                        continue
                    saved_threads.append(
                        write_thread(topic, synthesized, threads_dir=dirs["threads"], brain_dir=dirs["brain"])
                    )
                    seen_hashes.add(cluster_hash)

    briefing_result: dict[str, Any] | None = None
    if do_briefing:
        briefing_result = synthesize_weekly_briefing(
            project_scope=project_scope,
            days=days,
            brain_dir=dirs["brain"],
            model=model,
        )

    any_deferred = deferred_clusters > 0 or (briefing_result is not None and briefing_result.get("status") == "deferred")
    any_saved = bool(saved_threads) or (briefing_result is not None and briefing_result.get("status") == "saved")
    overall_status = "partial" if (any_deferred and any_saved) else ("deferred" if any_deferred else "completed")

    return {
        "status": overall_status,
        "saved_threads": saved_threads,
        "deferred_clusters": deferred_clusters,
        "skipped_clusters": skipped_clusters,
        "partitions": partition_labels,
        "briefing": briefing_result,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthesize approved Second Brain atoms into topic threads and weekly briefings.")
    brief_grp = parser.add_mutually_exclusive_group()
    brief_grp.add_argument("--briefing", dest="include_briefing", action="store_true", default=True, help="Include weekly briefing synthesis (default).")
    brief_grp.add_argument("--no-briefing", dest="include_briefing", action="store_false", help="Skip weekly briefing synthesis.")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument("--threads-only", action="store_true", help="Synthesize topic threads only.")
    mode_grp.add_argument("--briefing-only", action="store_true", help="Synthesize weekly briefing only.")
    parser.add_argument("--project", default=None, help="Restrict synthesis to a specific project:<slug> scope (or 'unscoped').")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days for weekly briefing episodes (default: 7).")
    parser.add_argument("--model", default=None, help="Preferred Ollama generation model.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON summary to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.json:
        with redirect_stdout(sys.stderr):
            result = run_editor(
                include_briefing=args.include_briefing,
                threads_only=args.threads_only,
                briefing_only=args.briefing_only,
                project_scope=args.project,
                days=args.days,
                model=args.model,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print("=== OLLAMA EDITOR ENGINE ===")
    result = run_editor(
        include_briefing=args.include_briefing,
        threads_only=args.threads_only,
        briefing_only=args.briefing_only,
        project_scope=args.project,
        days=args.days,
        model=args.model,
    )
    if result["deferred_clusters"] or (result["briefing"] and result["briefing"].get("status") == "deferred"):
        print(
            f"[!] Editor shift deferred unavailable model tasks cleanly "
            f"(saved_threads={len(result['saved_threads'])}, deferred_clusters={result['deferred_clusters']}, "
            f"briefing={result['briefing'].get('status') if result['briefing'] else 'skipped'})."
        )
    print("=== MORNING SHIFT COMPLETE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
