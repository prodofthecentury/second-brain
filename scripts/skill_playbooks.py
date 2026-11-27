# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Layer 4 Cognitive Core: Owner-gated Playbook/Skill promotion and cryptographic staleness re-gating."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("skill_playbooks", "scripts.skill_playbooks"):
    sys.modules.setdefault("skill_playbooks", sys.modules[__name__])
    sys.modules.setdefault("scripts.skill_playbooks", sys.modules[__name__])

from atom_mutator import review_lock
from cognitive_core import compute_memory_fitness_report, count_canonical_corrections
from config import (
    ATOMS_DIR,
    BRAIN_DIR,
    PLAYBOOKS_DIR,
    TELEMETRY_DB_PATH,
    _is_reparse_or_link,
    file_sha256,
    is_content_markdown,
    safe_file_in_dir,
)
from memory_format import (
    evaluate_temporal_validity,
    frontmatter_has_key,
    remove_frontmatter_fields,
    render_memory_document,
    set_scalar_field,
    split_frontmatter,
)
from memory_review import parse_frontmatter_values, parse_scalar_list, section_text
from recall import normalize_project_tag, project_tags_match
from review_atom import approved_revision_is_current

PLAYBOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
DISALLOWED_AUTOMATED_ACTORS = frozenset(
    {"auto", "system", "llm", "model", "ollama", "assistant", "agent", "agent_runtime"}
)
REQUIRED_PLAYBOOK_SECTIONS: tuple[str, ...] = (
    "Trigger & Applicability",
    "Preconditions & Inputs",
    "Ordered Steps",
    "Expected Outputs & Verification",
    "Failure Modes & Rollback",
)


class PlaybookValidationError(ValueError):
    """Raised when a playbook promotion, deprecation, or integrity check violates safety contracts."""


@dataclass(frozen=True)
class PlaybookIntegrityReport:
    playbook_id: str
    filename: str
    valid: bool
    status: str
    reason: str
    title: str = ""
    sha256: str = ""
    contexts: tuple[str, ...] = ()
    backing_atoms: tuple[str, ...] = ()
    trigger: str = ""
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    failure_recovery: tuple[str, ...] = ()
    # Per-backing-atom contexts (same order as backing_atoms) so scope checks can require
    # EVERY backing atom to be individually visible, rather than trusting the merged union.
    backing_atom_contexts: tuple[tuple[str, ...], ...] = ()


def playbook_decision_log_path(brain_dir: str) -> str:
    return os.path.join(brain_dir, "0-raw", "archive", "playbook-decisions.jsonl")


def _latest_playbook_decision(brain_dir: str, filename: str) -> tuple[bool, dict | None]:
    """Return (log_ok, latest_record_for_filename). log_ok=False means unsafe/unreadable (fail closed)."""
    log_path = playbook_decision_log_path(brain_dir)
    if not os.path.lexists(log_path):
        return True, None
    if _is_reparse_or_link(log_path) or _is_reparse_or_link(os.path.dirname(log_path)):
        return False, None
    latest: dict | None = None
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("filename") == filename:
                    latest = rec
    except OSError:
        return False, None
    return True, latest


def _project_scope_set(contexts) -> frozenset[str]:
    return frozenset(
        normalize_project_tag(str(c))
        for c in (contexts or ())
        if str(c).strip().lower().startswith("project:")
    )


def playbook_visible_in_scope(pb: PlaybookIntegrityReport, projects: tuple[str, ...]) -> bool:
    """A scoped caller may only see a Playbook if EVERY backing atom individually matches the scope.

    Mirrors recall semantics (unscoped atoms are excluded from scoped queries) and prevents a
    Playbook from laundering one project's atoms into another project's scope via a merged union.
    """
    clean = tuple(p for p in projects if str(p or "").strip())
    if not clean:
        return True
    if not pb.backing_atom_contexts or len(pb.backing_atom_contexts) != len(pb.backing_atoms):
        return False
    return all(bool(project_tags_match(tuple(ctx), clean)) for ctx in pb.backing_atom_contexts)


def _atom_contexts_from_frontmatter(fm: dict) -> tuple[str, ...]:
    raw = fm.get("contexts") or []
    if isinstance(raw, str):
        raw = parse_scalar_list(raw)
    return tuple(str(c).strip() for c in raw if str(c).strip())


def _sanitize_line(value: str, *, max_len: int = 500) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch in ("\r", "\n", "\t") or ord(ch) >= 32)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned).replace("-->", "—>").strip()
    if cleaned == "---":
        cleaned = "—--"
    return cleaned[:max_len]


def _normalize_playbook_id(raw_id: str) -> str:
    cleaned = str(raw_id or "").strip()
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    if not PLAYBOOK_ID_RE.fullmatch(cleaned):
        raise PlaybookValidationError(
            f"Invalid playbook_id {raw_id!r}: must match ^[a-z0-9][a-z0-9_-]{{1,63}}$ without path separators."
        )
    return cleaned


def _validate_human_actor(actor: str) -> str:
    cleaned = _sanitize_line(actor, max_len=120)
    lowered = cleaned.lower()
    prefix = lowered.split(":", 1)[0].strip()
    if not cleaned or lowered in DISALLOWED_AUTOMATED_ACTORS or prefix in DISALLOWED_AUTOMATED_ACTORS:
        raise PlaybookValidationError(
            f"Playbook mutation requires an explicit human owner actor (got {actor!r})."
        )
    return cleaned


def _load_telemetry_counts(atom_id: str, telemetry_db_path: str) -> tuple[int, int] | None:
    """Return (grounded_citations, corrections) from the disposable telemetry DB.

    Missing DB -> (0, 0). Any sqlite error (e.g. lock timeout) -> None so callers fail closed.
    """
    if not telemetry_db_path or not os.path.isfile(telemetry_db_path):
        return (0, 0)
    try:
        with closing(sqlite3.connect(telemetry_db_path, timeout=3.0)) as conn:
            row = conn.execute(
                "SELECT grounded_citations, corrections FROM atom_telemetry WHERE atom_id = ?",
                (atom_id,),
            ).fetchone()
            if not row:
                return (0, 0)
            return (int(row[0] or 0), int(row[1] or 0))
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return (0, 0)
        return None
    except sqlite3.Error:
        return None


def _effective_corrections(atom_id: str, telemetry_db_path: str, brain_dir: str) -> int | None:
    """Corrections = max(durable canonical log, disposable telemetry). None when either is unverifiable."""
    tele = _load_telemetry_counts(atom_id, telemetry_db_path)
    canonical = count_canonical_corrections(brain_dir, atom_id)
    if tele is None or canonical is None:
        return None
    return max(int(tele[1]), int(canonical))


def _backup_playbook_revision(playbooks_dir: str, source_path: str, backup_name: str) -> None:
    """Copy a prior Playbook revision into playbooks/.history/ without following planted links."""
    hist_dir = os.path.join(playbooks_dir, ".history")
    if os.path.lexists(hist_dir) and _is_reparse_or_link(hist_dir):
        raise PermissionError("Refusing to write backup under symlinked playbooks/.history directory.")
    os.makedirs(hist_dir, exist_ok=True)
    dest = os.path.join(hist_dir, backup_name)
    if os.path.lexists(dest):
        if _is_reparse_or_link(dest) or not os.path.isfile(dest):
            raise PermissionError("Refusing to overwrite a symlinked or non-regular Playbook history backup.")
        # Identical revision (same SHA prefix) already backed up.
        return
    data = Path(source_path).read_bytes()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise OSError("Incomplete Playbook history backup write.")
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_playbook_decision(brain_dir: str, record: dict[str, object]) -> None:
    log_path = playbook_decision_log_path(brain_dir)
    archive_dir = os.path.dirname(log_path)
    os.makedirs(archive_dir, exist_ok=True)
    if _is_reparse_or_link(archive_dir) or (os.path.lexists(log_path) and _is_reparse_or_link(log_path)):
        raise PermissionError("Refusing to write playbook decision log through a symlink or reparse point.")
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete write to playbook-decisions.jsonl.")
        os.fsync(fd)
    finally:
        os.close(fd)


def _raw_section_block(body: str, heading: str) -> str:
    """Extract a markdown section without collapsing internal newlines."""
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*\r?\n(.*?)(?=^##\s+|\Z)",
        body or "",
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _parse_bullet_or_numbered_lines(section_body: str) -> tuple[str, ...]:
    items: list[str] = []
    for raw_line in (section_body or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", stripped)
        items.append((m.group(1) if m else stripped).strip())
    return tuple(item for item in items if item)


def _claim_grounding_units(claim_text: str) -> set[str]:
    """Whole owner-approved Claim units: the full Claim, each list item/line, and each full sentence."""
    units: set[str] = set()
    full = " ".join(str(claim_text or "").split())
    if full:
        units.add(full)
    for raw_line in str(claim_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", stripped)
        line_unit = " ".join((m.group(1) if m else stripped).split())
        if line_unit:
            units.add(line_unit)
        for sentence in re.split(r"(?<=[.!?])\s+", line_unit):
            sentence = sentence.strip()
            if sentence:
                units.add(sentence)
    for sentence in re.split(r"(?<=[.!?])\s+", full):
        sentence = sentence.strip()
        if sentence:
            units.add(sentence)
    return units


def _step_is_grounded_in_atoms(step_text: str, allowed_corpora: list[tuple[str, str]]) -> bool:
    """Require every Playbook step to EQUAL a whole Claim, Claim list item, or full Claim sentence.

    Substring matching is intentionally rejected: it could strip negations or context
    ("Never run X" -> "run X"). Evidence excerpts (verbatim, possibly untrusted web text)
    are never used as a grounding corpus for operational steps.
    """
    norm_step = " ".join(str(step_text or "").split())
    if len(norm_step) < 5:
        return False
    for claim_text, _evidence_text in allowed_corpora:
        if norm_step in _claim_grounding_units(claim_text):
            return True
    return False


def inspect_skill_candidates(
    *,
    atoms_dir: str = ATOMS_DIR,
    brain_dir: str = BRAIN_DIR,
    playbooks_dir: str = PLAYBOOKS_DIR,
    telemetry_db_path: str = TELEMETRY_DB_PATH,
) -> list[dict[str, object]]:
    """Return approved procedural atoms and their promotion readiness."""
    fitness = compute_memory_fitness_report(
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
        db_path=telemetry_db_path,
    )
    active_pbs = list_playbooks(
        playbooks_dir=playbooks_dir,
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        telemetry_db_path=telemetry_db_path,
        active_only=True,
    )
    promoted_atoms: set[str] = set()
    for pb in active_pbs:
        promoted_atoms.update(pb.backing_atoms)

    results: list[dict[str, object]] = []
    for item in fitness.get("procedure_maturity", []):
        if not isinstance(item, dict) or not item.get("approved_current"):
            continue
        atom_id = str(item["atom_id"])
        atom_path = safe_file_in_dir(atoms_dir, atom_id, allowed_exts={".md"}, must_exist=True)
        sha = file_sha256(atom_path) if atom_path else None
        results.append(
            {
                **item,
                "sha256": sha,
                "already_in_active_playbook": atom_id in promoted_atoms,
                "eligible_without_override": item.get("maturity_stage") in {"skill_candidate", "executable_skill"},
            }
        )
    return results


def promote_skill_playbook(
    *,
    playbook_id: str,
    title: str,
    backing_atoms: list[tuple[str, str]],
    trigger: str,
    preconditions: list[str],
    steps: list[str],
    expected_outputs: list[str],
    failure_recovery: list[str],
    actor: str,
    rationale: str = "",
    allow_early_owner_override: bool = False,
    acknowledge_corrections: bool = False,
    expected_playbook_sha256: str | None = None,
    allow_multi_project: bool = False,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str = PLAYBOOKS_DIR,
    telemetry_db_path: str = TELEMETRY_DB_PATH,
) -> dict[str, object]:
    """CAS-locked owner promotion of one or more approved procedural atoms into a human-readable Playbook."""
    norm_id = _normalize_playbook_id(playbook_id)
    clean_actor = _validate_human_actor(actor)
    clean_title = _sanitize_line(title, max_len=200)
    clean_trigger = _sanitize_line(trigger, max_len=500)
    clean_rationale = _sanitize_line(rationale, max_len=500)

    clean_pre = [_sanitize_line(x) for x in (preconditions or []) if _sanitize_line(x)]
    clean_steps = [_sanitize_line(x) for x in (steps or []) if _sanitize_line(x)]
    clean_outputs = [_sanitize_line(x) for x in (expected_outputs or []) if _sanitize_line(x)]
    clean_recovery = [_sanitize_line(x) for x in (failure_recovery or []) if _sanitize_line(x)]

    if not clean_title or not clean_trigger:
        raise PlaybookValidationError("Playbook requires non-empty title and trigger.")
    if not clean_pre or not clean_steps or not clean_outputs or not clean_recovery:
        raise PlaybookValidationError(
            "Playbook requires non-empty preconditions, steps, expected_outputs, and failure_recovery."
        )
    if not backing_atoms:
        raise PlaybookValidationError("Playbook promotion requires at least one backing procedural atom.")

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    os.makedirs(playbooks_dir, exist_ok=True)
    if _is_reparse_or_link(playbooks_dir):
        raise PermissionError("Refusing to write playbook under symlinked playbooks directory.")

    with review_lock(lock_path):
        pb_filename = f"{norm_id}.md"
        target_path = safe_file_in_dir(playbooks_dir, pb_filename, allowed_exts={".md"}, must_exist=False)
        if not target_path:
            raise PlaybookValidationError(f"Unsafe playbook target filename: {pb_filename!r}")
        if os.path.lexists(target_path) and _is_reparse_or_link(target_path):
            raise PermissionError("Refusing to overwrite symlinked playbook file.")

        verified_atom_hashes: dict[str, str] = {}
        ack_corrections_map: dict[str, int] = {}
        inherited_contexts: list[str] = []
        allowed_step_corpora: list[tuple[str, str]] = []
        backing_scope_sets: list[frozenset[str]] = []

        for raw_atom_name, expected_sha in backing_atoms:
            atom_name = str(raw_atom_name or "").strip()
            if not atom_name.endswith(".md"):
                atom_name += ".md"
            atom_path = safe_file_in_dir(atoms_dir, atom_name, allowed_exts={".md"}, must_exist=True)
            if not atom_path or not os.path.isfile(atom_path) or _is_reparse_or_link(atom_path):
                raise PlaybookValidationError(f"Backing procedural atom not found or unsafe: {raw_atom_name!r}")

            current_sha = file_sha256(atom_path)
            if not current_sha or current_sha != str(expected_sha or "").strip().lower():
                raise PlaybookValidationError(
                    f"CAS hash mismatch on backing atom '{atom_name}': expected {expected_sha}, found {current_sha}."
                )
            if not approved_revision_is_current(atom_path, brain_dir=brain_dir, atoms_dir=atoms_dir):
                raise PlaybookValidationError(
                    f"Backing atom '{atom_name}' does not have a current, source-anchored owner approval."
                )

            atom_text = Path(atom_path).read_text(encoding="utf-8")
            parsed = split_frontmatter(atom_text)
            if parsed is None:
                raise PlaybookValidationError(f"Backing atom '{atom_name}' is missing valid YAML frontmatter.")
            fm = parse_frontmatter_values(parsed[0])
            if str(fm.get("memory_type", "")) != "procedural":
                raise PlaybookValidationError(
                    f"Backing atom '{atom_name}' must have memory_type='procedural' (got {fm.get('memory_type')!r})."
                )
            if str(fm.get("epistemic_label", "")) == "preference" or fm.get("epistemic_contract"):
                raise PlaybookValidationError(
                    f"Subjective preference atom '{atom_name}' cannot be promoted as an operational Playbook."
                )
            tv_promote = evaluate_temporal_validity(
                str(fm.get("valid_from") or "").strip(),
                str(fm.get("valid_until") or "").strip(),
            )
            if not tv_promote["active"] or tv_promote["status"] != "active":
                raise PlaybookValidationError(
                    f"Backing atom '{atom_name}' is not temporally active (status={tv_promote['status']}); refusing Playbook promotion."
                )

            tele_counts = _load_telemetry_counts(atom_name, telemetry_db_path)
            corrections = _effective_corrections(atom_name, telemetry_db_path, brain_dir)
            if tele_counts is None or corrections is None:
                raise PlaybookValidationError(
                    f"Correction/citation state for backing atom '{atom_name}' is unverifiable "
                    "(telemetry locked or correction log unreadable); refusing promotion (fail closed)."
                )
            citations = int(tele_counts[0])
            if corrections > 0 and not (acknowledge_corrections and clean_rationale):
                raise PlaybookValidationError(
                    f"Backing atom '{atom_name}' has {corrections} recorded correction(s); "
                    "pass acknowledge_corrections=True with an explicit rationale after reviewing."
                )
            if citations < 3 and not (allow_early_owner_override and clean_rationale):
                raise PlaybookValidationError(
                    f"Backing atom '{atom_name}' has {citations}/3 grounded citations required for skill_candidate "
                    "promotion; pass allow_early_owner_override=True with a rationale to override."
                )

            atom_contexts = _atom_contexts_from_frontmatter(fm)
            backing_scope_sets.append(_project_scope_set(atom_contexts))
            for cs in atom_contexts:
                if cs not in inherited_contexts:
                    inherited_contexts.append(cs)

            body = "\n".join(parsed[1])
            allowed_step_corpora.append((_raw_section_block(body, "Claim"), _raw_section_block(body, "Evidence")))
            verified_atom_hashes[atom_name] = current_sha
            ack_corrections_map[atom_name] = corrections

        if len(set(backing_scope_sets)) > 1 and not (allow_multi_project and clean_rationale):
            raise PlaybookValidationError(
                "Backing procedural atoms have differing project scopes (or mix scoped and unscoped atoms); "
                "a Playbook must not merge scopes. Pass allow_multi_project=True with an explicit rationale "
                "to override (scoped callers will still require EVERY backing atom to match their scope)."
            )

        for idx, step in enumerate(clean_steps, start=1):
            if not _step_is_grounded_in_atoms(step, allowed_step_corpora):
                raise PlaybookValidationError(
                    f"Step {idx} must exactly equal a whole Claim, Claim list item, or full Claim sentence "
                    f"of a backing procedural atom: {step!r}"
                )

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        previous_sha: str | None = None
        if os.path.lexists(target_path):
            if not os.path.isfile(target_path) or _is_reparse_or_link(target_path):
                raise PermissionError(f"Refusing to overwrite symlink or non-regular file: {pb_filename}")
            existing_text = Path(target_path).read_text(encoding="utf-8")
            existing_parsed = split_frontmatter(existing_text)
            existing_fm = parse_frontmatter_values(existing_parsed[0]) if existing_parsed else {}
            if str(existing_fm.get("type", "")) != "playbook":
                raise PlaybookValidationError(
                    f"Refusing to overwrite non-playbook file '{pb_filename}' in playbooks directory."
                )
            previous_sha = file_sha256(target_path)
        if previous_sha:
            expected_pb_norm = str(expected_playbook_sha256 or "").strip().lower()
            if not expected_pb_norm or expected_pb_norm != previous_sha:
                raise PlaybookValidationError(
                    f"Existing playbook '{pb_filename}' requires matching expected_playbook_sha256 for CAS update "
                    f"(expected {expected_playbook_sha256!r}, found {previous_sha})."
                )
            _backup_playbook_revision(playbooks_dir, target_path, f"{norm_id}.{previous_sha[:12]}.md")
        elif expected_playbook_sha256:
            raise PlaybookValidationError(
                f"Playbook '{pb_filename}' does not exist yet; omit expected_playbook_sha256 for initial promotion."
            )

        md_lines = [
            "---",
            f"playbook_id: {json.dumps(norm_id)}",
            "type: playbook",
            "playbook_status: active",
            f"title: {json.dumps(clean_title, ensure_ascii=False)}",
            f"promoted_at: {json.dumps(now_iso)}",
            f"promoted_by: {json.dumps(clean_actor, ensure_ascii=False)}",
            f"early_override: {'true' if allow_early_owner_override else 'false'}",
            f"multi_project_override: {'true' if (allow_multi_project and len(set(backing_scope_sets)) > 1) else 'false'}",
            f"contexts: {json.dumps(inherited_contexts, ensure_ascii=False)}",
            f"backing_atoms: {json.dumps(sorted(verified_atom_hashes.keys()), ensure_ascii=False)}",
            f"backing_atom_sha256s: {json.dumps(verified_atom_hashes, sort_keys=True)}",
            f"acknowledged_corrections: {json.dumps(ack_corrections_map, sort_keys=True)}",
            "---",
            "",
            f"# {clean_title}",
            "",
            "## Trigger & Applicability",
            clean_trigger,
            "",
            "## Preconditions & Inputs",
            *(f"- {item}" for item in clean_pre),
            "",
            "## Ordered Steps",
            *(f"{idx}. {item}" for idx, item in enumerate(clean_steps, start=1)),
            "",
            "## Expected Outputs & Verification",
            *(f"- {item}" for item in clean_outputs),
            "",
            "## Failure Modes & Rollback",
            *(f"- {item}" for item in clean_recovery),
            "",
        ]
        encoded = "\n".join(md_lines).encode("utf-8")

        if previous_sha is None:
            o_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(target_path, o_flags, 0o600)
            except FileExistsError as exc:
                raise PlaybookValidationError(
                    f"Playbook '{pb_filename}' appeared concurrently during initial promotion."
                ) from exc
            try:
                if os.name != "nt":
                    os.chmod(target_path, 0o600)
                if os.write(fd, encoded) != len(encoded):
                    raise OSError("Incomplete playbook write.")
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            fd, tmp_path = tempfile.mkstemp(dir=playbooks_dir, prefix=f".{norm_id}-", suffix=".tmp")
            closed = False
            try:
                if os.name != "nt":
                    os.chmod(tmp_path, 0o600)
                if os.write(fd, encoded) != len(encoded):
                    raise OSError("Incomplete playbook write.")
                os.fsync(fd)
                os.close(fd)
                closed = True
                os.replace(tmp_path, target_path)
            finally:
                if not closed:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        new_sha = file_sha256(target_path) or ""
        decision = {
            "schema_version": 1,
            "timestamp": now_iso,
            "action": "promote_playbook",
            "playbook_id": norm_id,
            "filename": pb_filename,
            "previous_sha256": previous_sha,
            "new_sha256": new_sha,
            "backing_atom_sha256s": verified_atom_hashes,
            "actor": clean_actor,
            "allow_early_owner_override": bool(allow_early_owner_override),
            "allow_multi_project": bool(allow_multi_project and len(set(backing_scope_sets)) > 1),
            "rationale": clean_rationale,
        }
        _append_playbook_decision(brain_dir, decision)
        return {
            "playbook_id": norm_id,
            "filename": pb_filename,
            "sha256": new_sha,
            "status": "active",
            "contexts": inherited_contexts,
            "backing_atoms": sorted(verified_atom_hashes.keys()),
        }


def verify_playbook_integrity(
    playbook_path: str,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str = PLAYBOOKS_DIR,
    telemetry_db_path: str = TELEMETRY_DB_PATH,
) -> PlaybookIntegrityReport:
    """Verify Playbook schema, backing procedural atom SHA-256 & owner approvals, and correction bounds."""
    fname = os.path.basename(playbook_path)
    pb_id_fallback = os.path.splitext(fname)[0]
    if _is_reparse_or_link(playbooks_dir):
        return PlaybookIntegrityReport(pb_id_fallback, fname, False, "invalid", "symlinked_playbooks_directory")
    resolved = safe_file_in_dir(playbooks_dir, fname, allowed_exts={".md"}, must_exist=True)
    if not resolved or not os.path.isfile(resolved) or _is_reparse_or_link(resolved):
        return PlaybookIntegrityReport(pb_id_fallback, fname, False, "invalid", "missing_or_unsafe_playbook_file")

    try:
        raw_bytes = Path(resolved).read_bytes()
        raw_text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return PlaybookIntegrityReport(pb_id_fallback, fname, False, "invalid", "unreadable_playbook_file")
    # Hash the exact bytes that are parsed below (no TOCTOU between hashing and parsing).
    sha = hashlib.sha256(raw_bytes).hexdigest()

    parsed = split_frontmatter(raw_text)
    if parsed is None:
        return PlaybookIntegrityReport(pb_id_fallback, fname, False, "invalid", "missing_frontmatter", sha256=sha)

    raw_fm = parse_frontmatter_values(parsed[0])
    fm_map: dict[str, object] = dict(raw_fm)
    for json_dict_key in ("backing_atom_sha256s", "acknowledged_corrections"):
        raw_mapping = fm_map.get(json_dict_key)
        if isinstance(raw_mapping, str) and raw_mapping.strip().startswith("{"):
            try:
                fm_map[json_dict_key] = json.loads(raw_mapping)
            except json.JSONDecodeError:
                pass

    pb_id = str(fm_map.get("playbook_id") or pb_id_fallback)
    status = str(fm_map.get("playbook_status") or "invalid")
    title = str(fm_map.get("title") or pb_id)
    contexts_raw = fm_map.get("contexts")
    if isinstance(contexts_raw, str):
        contexts_raw = parse_scalar_list(contexts_raw)
    contexts = tuple(str(x).strip() for x in contexts_raw if str(x).strip()) if isinstance(contexts_raw, list) else ()
    backing_list = fm_map.get("backing_atoms")
    if isinstance(backing_list, str):
        backing_list = parse_scalar_list(backing_list)
    backing_hashes = fm_map.get("backing_atom_sha256s")
    ack_corr = fm_map.get("acknowledged_corrections")
    if not isinstance(ack_corr, dict):
        ack_corr = {}

    body = "\n".join(parsed[1])
    for sec in REQUIRED_PLAYBOOK_SECTIONS:
        if not _raw_section_block(body, sec).strip():
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"missing_required_section:{sec}", title=title, sha256=sha
            )

    trigger = _sanitize_line(section_text(body, "Trigger & Applicability"))
    preconditions = _parse_bullet_or_numbered_lines(_raw_section_block(body, "Preconditions & Inputs"))
    steps = _parse_bullet_or_numbered_lines(_raw_section_block(body, "Ordered Steps"))
    expected_outputs = _parse_bullet_or_numbered_lines(_raw_section_block(body, "Expected Outputs & Verification"))
    failure_recovery = _parse_bullet_or_numbered_lines(_raw_section_block(body, "Failure Modes & Rollback"))

    if status != "active":
        return PlaybookIntegrityReport(
            pb_id,
            fname,
            False,
            status,
            f"playbook_not_active:{status}",
            title=title,
            sha256=sha,
            contexts=contexts,
            backing_atoms=tuple(backing_list) if isinstance(backing_list, list) else (),
            trigger=trigger,
            preconditions=preconditions,
            steps=steps,
            expected_outputs=expected_outputs,
            failure_recovery=failure_recovery,
        )

    if not isinstance(backing_list, list) or not backing_list or not isinstance(backing_hashes, dict):
        return PlaybookIntegrityReport(pb_id, fname, False, status, "missing_backing_atom_anchors", title=title, sha256=sha)

    # H1: the frontmatter playbook_id must equal the filename stem (prevents matched_playbook_ids spoofing).
    if pb_id != pb_id_fallback:
        return PlaybookIntegrityReport(
            pb_id_fallback, fname, False, status, "playbook_id_filename_mismatch", title=title, sha256=sha
        )

    # H1: the exact file revision must be the one recorded by the latest owner promote decision.
    # Out-of-band edits (un-deprecation, acknowledged_corrections inflation, context injection,
    # rewritten preconditions/rollback text) change the SHA and therefore fail closed here.
    log_ok, latest_decision = _latest_playbook_decision(brain_dir, fname)
    if not log_ok:
        return PlaybookIntegrityReport(
            pb_id, fname, False, status, "playbook_decision_log_unreadable", title=title, sha256=sha
        )
    if (
        latest_decision is None
        or latest_decision.get("action") != "promote_playbook"
        or str(latest_decision.get("new_sha256") or "") != sha
    ):
        reason = (
            "playbook_deprecated_in_decision_log"
            if latest_decision is not None and latest_decision.get("action") == "deprecate_playbook"
            else "playbook_revision_not_owner_promoted"
        )
        return PlaybookIntegrityReport(pb_id, fname, False, status, reason, title=title, sha256=sha)

    corpora: list[tuple[str, str]] = []
    merged_contexts = list(contexts)
    per_atom_contexts: list[tuple[str, ...]] = []
    for atom_name_raw in backing_list:
        atom_name = str(atom_name_raw)
        expected_atom_sha = str(backing_hashes.get(atom_name, ""))
        atom_path = safe_file_in_dir(atoms_dir, atom_name, allowed_exts={".md"}, must_exist=True)
        if not atom_path or not os.path.isfile(atom_path) or _is_reparse_or_link(atom_path):
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"missing_backing_atom:{atom_name}", title=title, sha256=sha
            )
        actual_atom_sha = file_sha256(atom_path) or ""
        if not expected_atom_sha or actual_atom_sha != expected_atom_sha:
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"stale_backing_atom_sha256:{atom_name}", title=title, sha256=sha
            )
        if not approved_revision_is_current(atom_path, brain_dir=brain_dir, atoms_dir=atoms_dir):
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"backing_atom_approval_not_current:{atom_name}", title=title, sha256=sha
            )
        current_corr = _effective_corrections(atom_name, telemetry_db_path, brain_dir)
        if current_corr is None:
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"correction_state_unverifiable:{atom_name}", title=title, sha256=sha
            )
        allowed_corr = int(ack_corr.get(atom_name, 0) or 0)
        if current_corr > allowed_corr:
            return PlaybookIntegrityReport(
                pb_id,
                fname,
                False,
                status,
                f"unacknowledged_corrections:{atom_name}({current_corr}>{allowed_corr})",
                title=title,
                sha256=sha,
            )

        atom_text = Path(atom_path).read_text(encoding="utf-8")
        atom_parsed = split_frontmatter(atom_text)
        if atom_parsed is None:
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"corrupt_backing_atom:{atom_name}", title=title, sha256=sha
            )
        atom_fm = parse_frontmatter_values(atom_parsed[0])
        if str(atom_fm.get("memory_type", "semantic")) != "procedural":
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"non_procedural_backing_atom:{atom_name}", title=title, sha256=sha
            )
        if str(atom_fm.get("epistemic_label", "claim")) == "preference" or bool(atom_fm.get("epistemic_contract")):
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"preference_backing_atom_forbidden:{atom_name}", title=title, sha256=sha
            )
        tv_verify = evaluate_temporal_validity(
            str(atom_fm.get("valid_from") or "").strip(),
            str(atom_fm.get("valid_until") or "").strip(),
        )
        if not tv_verify["active"] or tv_verify["status"] != "active":
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, f"backing_atom_not_temporally_active:{atom_name}({tv_verify['status']})", title=title, sha256=sha
            )
        atom_ctx = _atom_contexts_from_frontmatter(atom_fm)
        per_atom_contexts.append(atom_ctx)
        for cs in atom_ctx:
            if cs not in merged_contexts:
                merged_contexts.append(cs)
        atom_body = "\n".join(atom_parsed[1])
        corpora.append((_raw_section_block(atom_body, "Claim"), _raw_section_block(atom_body, "Evidence")))

    for step in steps:
        if not _step_is_grounded_in_atoms(step, corpora):
            return PlaybookIntegrityReport(
                pb_id, fname, False, status, "ungrounded_playbook_step", title=title, sha256=sha
            )

    return PlaybookIntegrityReport(
        playbook_id=pb_id,
        filename=fname,
        valid=True,
        status=status,
        reason="verified_active",
        title=title,
        sha256=sha,
        contexts=tuple(merged_contexts),
        backing_atoms=tuple(str(x) for x in backing_list),
        trigger=trigger,
        preconditions=preconditions,
        steps=steps,
        expected_outputs=expected_outputs,
        failure_recovery=failure_recovery,
        backing_atom_contexts=tuple(per_atom_contexts),
    )


def list_playbooks(
    *,
    playbooks_dir: str = PLAYBOOKS_DIR,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    telemetry_db_path: str = TELEMETRY_DB_PATH,
    active_only: bool = True,
) -> list[PlaybookIntegrityReport]:
    """List Playbooks in brain/5-playbooks/ with real-time cryptographic integrity verification."""
    if not os.path.isdir(playbooks_dir) or _is_reparse_or_link(playbooks_dir):
        return []
    reports: list[PlaybookIntegrityReport] = []
    for fname in sorted(os.listdir(playbooks_dir)):
        if not is_content_markdown(fname):
            continue
        rep = verify_playbook_integrity(
            os.path.join(playbooks_dir, fname),
            brain_dir=brain_dir,
            atoms_dir=atoms_dir,
            playbooks_dir=playbooks_dir,
            telemetry_db_path=telemetry_db_path,
        )
        if active_only and not rep.valid:
            continue
        reports.append(rep)
    return reports


def deprecate_skill_playbook(
    playbook_filename: str,
    *,
    expected_sha256: str,
    actor: str,
    rationale: str,
    brain_dir: str = BRAIN_DIR,
    playbooks_dir: str = PLAYBOOKS_DIR,
) -> dict[str, object]:
    """CAS-locked owner deprecation of an existing Playbook."""
    clean_actor = _validate_human_actor(actor)
    clean_rationale = _sanitize_line(rationale, max_len=500)
    if not clean_rationale:
        raise PlaybookValidationError("Playbook deprecation requires a non-empty rationale.")

    fname = str(playbook_filename or "").strip()
    if not fname.endswith(".md"):
        fname += ".md"

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    with review_lock(lock_path):
        if _is_reparse_or_link(playbooks_dir):
            raise PermissionError("Refusing to deprecate playbook under symlinked playbooks directory.")
        target = safe_file_in_dir(playbooks_dir, fname, allowed_exts={".md"}, must_exist=True)
        if not target or not os.path.isfile(target) or _is_reparse_or_link(target):
            raise PlaybookValidationError(f"Playbook not found or unsafe: {playbook_filename!r}")
        current_sha = file_sha256(target) or ""
        if current_sha != str(expected_sha256 or "").strip().lower():
            raise PlaybookValidationError(
                f"CAS hash mismatch on playbook '{fname}': expected {expected_sha256}, found {current_sha}."
            )

        raw_text = Path(target).read_text(encoding="utf-8")
        parsed_pb = split_frontmatter(raw_text)
        if parsed_pb is None:
            raise PlaybookValidationError(f"Playbook '{fname}' is missing a valid frontmatter block.")
        existing_fm = parse_frontmatter_values(parsed_pb[0])
        if str(existing_fm.get("type", "")).strip() != "playbook":
            raise PlaybookValidationError(f"File '{fname}' is not a Skill Playbook (missing type: playbook).")
        if not frontmatter_has_key(parsed_pb[0], "playbook_status"):
            raise PlaybookValidationError(f"Playbook '{fname}' is missing playbook_status field.")
        cur_status = str(existing_fm.get("playbook_status", "")).strip()
        if cur_status != "active":
            raise PlaybookValidationError(f"Playbook '{fname}' is not active (status={cur_status!r}).")

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        updated_fm = remove_frontmatter_fields(parsed_pb[0], {"deprecated_at", "deprecated_by"})
        updated_fm = set_scalar_field(updated_fm, "playbook_status", "deprecated", "type")
        updated_fm = set_scalar_field(updated_fm, "deprecated_at", json.dumps(now_iso), "acknowledged_corrections")
        updated_fm = set_scalar_field(
            updated_fm, "deprecated_by", json.dumps(clean_actor, ensure_ascii=False), "deprecated_at"
        )
        updated_text = render_memory_document(updated_fm, parsed_pb[1])

        reparsed = split_frontmatter(updated_text)
        reparsed_fm = parse_frontmatter_values(reparsed[0]) if reparsed is not None else {}
        if str(reparsed_fm.get("playbook_status", "")) != "deprecated":
            raise PlaybookValidationError(f"Playbook '{fname}' deprecation could not be verified after edit.")

        _backup_playbook_revision(playbooks_dir, target, f"{os.path.splitext(fname)[0]}.{current_sha[:12]}.md")

        encoded = updated_text.encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(dir=playbooks_dir, prefix=".dep-pb-", suffix=".tmp")
        closed = False
        try:
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            if os.write(fd, encoded) != len(encoded):
                raise OSError("Incomplete playbook deprecation write.")
            os.fsync(fd)
            os.close(fd)
            closed = True
            os.replace(tmp_path, target)
        finally:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        new_sha = file_sha256(target) or ""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        _append_playbook_decision(
            brain_dir,
            {
                "schema_version": 1,
                "timestamp": now_iso,
                "action": "deprecate_playbook",
                "filename": fname,
                "previous_sha256": current_sha,
                "new_sha256": new_sha,
                "actor": clean_actor,
                "rationale": clean_rationale,
            },
        )
        return {"filename": fname, "status": "deprecated", "sha256": new_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage owner-gated Second Brain Skill Playbooks.")
    sub = parser.add_subparsers(dest="command", required=True)

    cand_p = sub.add_parser("candidates", help="Inspect approved procedural atoms and promotion readiness.")
    cand_p.add_argument("--json", action="store_true")

    list_p = sub.add_parser("list", help="List Playbooks and verify cryptographic backing anchors.")
    list_p.add_argument("--all", action="store_true", help="Include stale or deprecated playbooks.")
    list_p.add_argument("--json", action="store_true")

    dep_p = sub.add_parser("deprecate", help="Deprecate an active Playbook using CAS hash verification.")
    dep_p.add_argument("filename")
    dep_p.add_argument("--expected-sha256", required=True)
    dep_p.add_argument("--actor", required=True)
    dep_p.add_argument("--rationale", required=True)

    prom_p = sub.add_parser("promote", help="Promote approved procedural atom(s) into a CAS-locked Playbook.")
    prom_p.add_argument("--playbook-id", required=True)
    prom_p.add_argument("--title", required=True)
    prom_p.add_argument(
        "--atom",
        action="append",
        required=True,
        help="Backing procedural atom in <filename>:<expected_sha256> format (repeatable).",
    )
    prom_p.add_argument("--trigger", required=True)
    prom_p.add_argument("--precondition", action="append", required=True)
    prom_p.add_argument("--step", action="append", required=True)
    prom_p.add_argument("--expected-output", action="append", required=True)
    prom_p.add_argument("--failure-recovery", action="append", required=True)
    prom_p.add_argument("--actor", required=True)
    prom_p.add_argument("--rationale", default="")
    prom_p.add_argument("--allow-early-override", action="store_true")
    prom_p.add_argument("--acknowledge-corrections", action="store_true")
    prom_p.add_argument(
        "--allow-multi-project",
        action="store_true",
        help="Allow backing atoms with differing project scopes (requires --rationale).",
    )
    prom_p.add_argument("--expected-playbook-sha256", default=None)

    args = parser.parse_args(argv)
    if args.command == "candidates":
        cands = inspect_skill_candidates()
        if args.json:
            print(json.dumps(cands, indent=2, sort_keys=True))
        else:
            if not cands:
                print("No approved procedural atoms currently present.")
            for c in cands:
                print(
                    f"{c['atom_id']:32} | stage={c['maturity_stage']:18} | "
                    f"citations={c['grounded_citations']} | corrections={c['corrections']} | sha256={str(c['sha256'])[:12]}"
                )
        return 0

    if args.command == "list":
        pbs = list_playbooks(active_only=not args.all)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "playbook_id": p.playbook_id,
                            "filename": p.filename,
                            "valid": p.valid,
                            "status": p.status,
                            "reason": p.reason,
                            "title": p.title,
                            "sha256": p.sha256,
                            "contexts": list(p.contexts),
                            "backing_atoms": list(p.backing_atoms),
                        }
                        for p in pbs
                    ],
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            if not pbs:
                print("No matching playbooks found.")
            for p in pbs:
                badge = "ACTIVE" if p.valid else f"INACTIVE({p.reason})"
                print(f"[{badge}] {p.playbook_id:28} | {p.title} | atoms={','.join(p.backing_atoms)}")
        return 0

    if args.command == "deprecate":
        res = deprecate_skill_playbook(
            args.filename,
            expected_sha256=args.expected_sha256,
            actor=args.actor,
            rationale=args.rationale,
        )
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0

    if args.command == "promote":
        backing_pairs: list[tuple[str, str]] = []
        for raw_spec in args.atom:
            if ":" not in raw_spec:
                raise PlaybookValidationError(f"--atom must be '<filename>:<sha256>', got {raw_spec!r}")
            fname, sha = raw_spec.rsplit(":", 1)
            backing_pairs.append((fname.strip(), sha.strip()))
        res = promote_skill_playbook(
            playbook_id=args.playbook_id,
            title=args.title,
            backing_atoms=backing_pairs,
            trigger=args.trigger,
            preconditions=list(args.precondition),
            steps=list(args.step),
            expected_outputs=list(args.expected_output),
            failure_recovery=list(args.failure_recovery),
            actor=args.actor,
            rationale=args.rationale,
            allow_early_owner_override=args.allow_early_override,
            acknowledge_corrections=args.acknowledge_corrections,
            expected_playbook_sha256=args.expected_playbook_sha256,
            allow_multi_project=args.allow_multi_project,
        )
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
