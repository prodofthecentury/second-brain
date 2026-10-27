# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Single canonical mutation path for atom edits and owner review decisions."""

from __future__ import annotations

from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from config import ATOMS_DIR, RAW_DIR
from evidence import ANCHOR_FIELDS, EvidenceValidationError, find_evidence_anchor, verify_evidence_anchor
from memory_format import (
    frontmatter_value,
    is_memory_document,
    normalize_memory_document,
    render_memory_document,
    set_scalar_field,
    split_frontmatter,
)
from memory_review import parse_frontmatter_values


DECISION_LOG = os.path.join(RAW_DIR, "archive", "decision-record.jsonl")
ATOM_HISTORY_DIR = os.path.join(ATOMS_DIR, ".history")
TRANSITIONS = {
    "approve": {"claim_state": "human_reviewed", "review_status": "accepted"},
    "reject": {"claim_state": "rejected", "review_status": "rejected"},
    "quarantine": {"claim_state": "quarantined", "review_status": "needs_edit"},
    "demote": {"claim_state": "candidate", "review_status": "pending"},
}


class ReviewConflictError(RuntimeError):
    """The atom changed since the owner inspected the revision they meant to change."""


class ReviewTransitionError(RuntimeError):
    """The requested mutation does not satisfy the required integrity gates."""


@contextmanager
def review_lock(lock_path: str):
    """Use one OS advisory lock for all local canonical and derived-state writers."""
    directory = os.path.dirname(os.path.abspath(lock_path))
    os.makedirs(directory, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def atomic_write_bytes(path: str, content: bytes):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".atom-write-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def append_decision_record(record: dict, decision_log_path: str):
    directory = os.path.dirname(os.path.abspath(decision_log_path))
    os.makedirs(directory, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(decision_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("Decision record append was incomplete.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def refresh_lexical_index(atoms_dir: str, brain_dir: str) -> dict:
    """Refresh the rebuildable model-free index while the shared writer lock is held."""
    from lexical_store import rebuild_lexical_index

    index_path = os.path.join(brain_dir, ".sidecar", "lexical.sqlite3")
    return rebuild_lexical_index(atoms_dir, index_path)


def _canonical_atom_path(atom_path: str, atoms_dir: str) -> str:
    try:
        root = Path(atoms_dir).resolve(strict=True)
        path = Path(atom_path).resolve(strict=True)
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ReviewTransitionError("Atom path must remain inside the canonical atoms directory.")
        if not path.is_file() or path.suffix.lower() != ".md":
            raise ReviewTransitionError("Atom path must be an existing Markdown file.")
        return str(path)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ReviewTransitionError):
            raise
        raise ReviewTransitionError(f"Atom path is invalid: {exc}") from exc


def _write_mutation(
    *,
    atom_path: str,
    original: bytes,
    updated: bytes,
    action: str,
    actor: str,
    decision_log_path: str,
    history_dir: str,
    brain_dir: str,
    evidence_anchor: dict | None = None,
    dense_refresh_required: bool = False,
    dense_model: str | None = None,
) -> dict:
    before_hash = content_sha256(original)
    after_hash = content_sha256(updated)
    os.makedirs(history_dir, exist_ok=True)
    backup_path = os.path.join(history_dir, f"{Path(atom_path).name}.{before_hash}.bak")
    if not os.path.exists(backup_path):
        atomic_write_bytes(backup_path, original)

    atomic_write_bytes(atom_path, updated)
    try:
        index_result = refresh_lexical_index(os.path.dirname(atom_path), brain_dir)
        dense_state = "not_active"
        dense_generation = None
        dense_error = None
        if dense_refresh_required:
            try:
                from rebuild_vectors import rebuild_vectors

                dense_result = rebuild_vectors(
                    atoms_dir=os.path.dirname(atom_path),
                    store_path=os.path.join(brain_dir, ".chroma"),
                    collection_name="atoms",
                    model=dense_model,
                    _writer_lock_held=True,
                )
                dense_state = "current" if dense_result.get("committed") else "stale"
                dense_generation = dense_result.get("generation_id") if dense_state == "current" else None
            except Exception as exc:
                dense_state = "stale"
                dense_error = str(exc)[:1000]
        record = {
            "schema_version": 1,
            "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "actor": actor.strip(),
            "atom_filename": Path(atom_path).name,
            "content_sha256_before": before_hash,
            "content_sha256_after": after_hash,
            "backup_path": backup_path,
            "lexical_index_path": index_result["index_path"],
            "dense_index_state": dense_state,
        }
        if dense_generation:
            record["dense_index_generation"] = dense_generation
        if dense_error:
            record["dense_index_error"] = dense_error
        if evidence_anchor:
            record.update(evidence_anchor)
        append_decision_record(record, decision_log_path)
    except Exception:
        atomic_write_bytes(atom_path, original)
        try:
            refresh_lexical_index(os.path.dirname(atom_path), brain_dir)
        except Exception:
            pass
        raise
    return record


def apply_atom_mutation(
    *,
    action: str,
    atom_path: str,
    expected_sha256: str,
    actor: str,
    edited_content: str | None = None,
    decision_log_path: str = DECISION_LOG,
    history_dir: str | None = None,
    brain_dir: str | None = None,
    atoms_dir: str = ATOMS_DIR,
    reviewed_on: str | None = None,
) -> dict:
    """Compare, mutate, reindex, and attest one atom revision as one serialized operation."""
    atoms_dir = os.path.abspath(atoms_dir)
    brain_dir = os.path.abspath(brain_dir or os.path.dirname(atoms_dir))
    atom_path = _canonical_atom_path(atom_path, atoms_dir)
    decision_log_path = os.path.abspath(decision_log_path)
    history_dir = os.path.abspath(history_dir or os.path.join(atoms_dir, ".history"))

    if not actor or not actor.strip():
        raise ReviewTransitionError("An explicit owner actor is required for atom mutations.")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ReviewConflictError("An exact 64-character expected SHA-256 is required for atom mutations.")
    if action not in {*TRANSITIONS, "edit"}:
        raise ReviewTransitionError(f"Unknown atom mutation action: {action}")
    if action == "edit" and edited_content is None:
        raise ReviewTransitionError("Edited Markdown content is required for an edit mutation.")
    if action != "edit" and edited_content is not None:
        raise ReviewTransitionError("Edited content is allowed only for the edit mutation.")

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    with review_lock(lock_path):
        try:
            original = Path(atom_path).read_bytes()
        except OSError as exc:
            raise ReviewTransitionError(f"Could not read atom for mutation: {exc}") from exc
        actual_hash = content_sha256(original)
        if actual_hash != expected_sha256:
            raise ReviewConflictError(
                f"Atom revision changed (expected {expected_sha256}, found {actual_hash}); reopen it before mutation."
            )
        try:
            original_text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReviewTransitionError("Atom is not valid UTF-8 Markdown.") from exc
        parsed = split_frontmatter(original_text)
        if parsed is None or not is_memory_document(original_text):
            raise ReviewTransitionError("Atom must contain valid frontmatter, Claim, and Evidence sections.")
        frontmatter, body = parsed

        from index_manifest import index_manifest_is_current, load_index_manifest

        active_dense_manifest = load_index_manifest(os.path.join(brain_dir, ".chroma"))
        dense_refresh_required = (
            isinstance(active_dense_manifest, dict)
            and active_dense_manifest.get("distance_metric") == "cosine"
            and isinstance(active_dense_manifest.get("embedding_model"), str)
            and bool(active_dense_manifest.get("embedding_model"))
            and isinstance(active_dense_manifest.get("embedding_dimensions"), int)
            and bool(active_dense_manifest.get("generation_id"))
            and index_manifest_is_current(active_dense_manifest, atoms_dir)
        )

        evidence_anchor = None
        if action == "edit":
            try:
                edited_text = str(edited_content).encode("utf-8").decode("utf-8")
            except UnicodeEncodeError as exc:
                raise ReviewTransitionError("Atom content must be valid UTF-8 Markdown.") from exc
            if split_frontmatter(edited_text) is None or not is_memory_document(edited_text):
                raise ReviewTransitionError("Atom edits must retain valid frontmatter, Claim, and Evidence sections.")
            current_values = parse_frontmatter_values(frontmatter)
            source_origin = str(current_values.get("origin", "imported_unknown"))
            updated_text = normalize_memory_document(
                edited_text,
                source_filename=Path(atom_path).name,
                current_date=datetime.datetime.now().strftime("%Y-%m-%d"),
                origin=source_origin,
            )
            updated = updated_text.encode("utf-8")
        else:
            if action == "approve":
                if frontmatter_value(frontmatter, "epistemic_label") in {"hypothesis", "preference"}:
                    raise ReviewTransitionError("Hypotheses and preferences cannot be promoted to factual standard recall.")
                try:
                    evidence_anchor = find_evidence_anchor(original_text, brain_dir)
                except (EvidenceValidationError, OSError, ValueError) as exc:
                    raise ReviewTransitionError(f"Approval requires verifiable source evidence: {exc}") from exc

            reviewed_on = reviewed_on or datetime.datetime.now().strftime("%Y-%m-%d")
            transition = TRANSITIONS[action]
            updated_frontmatter = set_scalar_field(frontmatter, "claim_state", transition["claim_state"], "citation_integrity")
            updated_frontmatter = set_scalar_field(updated_frontmatter, "review_status", transition["review_status"], "epistemic_label")
            updated_frontmatter = set_scalar_field(updated_frontmatter, "last-reviewed", reviewed_on, "created")
            if evidence_anchor:
                updated_frontmatter = set_scalar_field(updated_frontmatter, "citation_integrity", "anchored", "origin")
                for key in ANCHOR_FIELDS:
                    updated_frontmatter = set_scalar_field(updated_frontmatter, key, str(evidence_anchor[key]), "citation_integrity")
            updated = render_memory_document(updated_frontmatter, body).encode("utf-8")

        return _write_mutation(
            atom_path=atom_path,
            original=original,
            updated=updated,
            action=action,
            actor=actor,
            decision_log_path=decision_log_path,
            history_dir=history_dir,
            brain_dir=brain_dir,
            evidence_anchor=evidence_anchor,
            dense_refresh_required=dense_refresh_required,
            dense_model=active_dense_manifest.get("embedding_model") if dense_refresh_required else None,
        )


def apply_atom_edit(**kwargs) -> dict:
    """Compatibility wrapper; all behavior lives in apply_atom_mutation."""
    return apply_atom_mutation(action="edit", **kwargs)


def apply_review_transition(*, action: str, **kwargs) -> dict:
    """Compatibility wrapper; all behavior lives in apply_atom_mutation."""
    return apply_atom_mutation(action=action, **kwargs)


def approved_revision_is_current(
    atom_path: str,
    decision_log_path: str = DECISION_LOG,
    brain_dir: str | None = None,
    atoms_dir: str = ATOMS_DIR,
) -> bool:
    """Accept only the latest owner approval for the exact atom and evidence revisions."""
    try:
        brain_dir = os.path.abspath(brain_dir or os.path.dirname(os.path.abspath(atoms_dir)))
        path = _canonical_atom_path(atom_path, atoms_dir)
        content_bytes = Path(path).read_bytes()
        content = content_bytes.decode("utf-8")
        current_hash = content_sha256(content_bytes)
        parsed = split_frontmatter(content)
        if parsed is None or not is_memory_document(content):
            return False
        frontmatter, _ = parsed
        values = parse_frontmatter_values(frontmatter)
        if values.get("review_status") != "accepted" or values.get("claim_state") != "human_reviewed":
            return False
        anchors = {key: values.get(key) for key in ANCHOR_FIELDS}
        if not all(anchors.values()) or frontmatter_value(frontmatter, "citation_integrity") != "anchored":
            return False
        with open(decision_log_path, "r", encoding="utf-8") as file:
            records = [json.loads(line) for line in file if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ReviewTransitionError):
        return False

    latest = next((record for record in reversed(records) if record.get("atom_filename") == Path(path).name), None)
    if not latest or latest.get("action") != "approve" or latest.get("content_sha256_after") != current_hash:
        return False
    return verify_evidence_anchor(content, anchors, brain_dir)
