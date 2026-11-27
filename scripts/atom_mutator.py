# /// script
# requires-python = ">=3.11,<3.14"
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
import stat
import tempfile
import threading
import time

from scripts.config import (
    ATOMS_DIR,
    BRAIN_DIR,
    RAW_DIR,
    _is_reparse_or_link,
    resolve_module_vault_path,
    snapshot_module_defaults,
)

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
from scripts.evidence import (
    ANCHOR_FIELDS,
    EvidenceValidationError,
    claim_entailed_by_evidence,
    extract_evidence_from_body_lines,
    find_evidence_anchor,
    has_cached_normalized_source,
    verify_evidence_anchor,
    verify_evidence_anchor_known_source,
)
from scripts.index_manifest import LARGE_VAULT_STAT_CACHE_THRESHOLD, record_known_atom_hash
from scripts.memory_format import (
    evaluate_temporal_validity,
    frontmatter_value,
    is_memory_document,
    normalize_memory_document,
    normalize_temporal_bound_str,
    remove_frontmatter_fields,
    render_memory_document,
    set_scalar_field,
    set_string_list_field,
    split_frontmatter,
)
from scripts.memory_review import parse_frontmatter_values, parse_scalar_list, section_text


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


def _validate_frontmatter_temporal_bounds(fm_lines: list[str]) -> tuple[str, str]:
    raw_vf = frontmatter_value(fm_lines, "valid_from")
    raw_vu = frontmatter_value(fm_lines, "valid_until")
    try:
        norm_vf = normalize_temporal_bound_str(raw_vf) if raw_vf else ""
        norm_vu = normalize_temporal_bound_str(raw_vu) if raw_vu else ""
    except ValueError as exc:
        raise ReviewTransitionError(f"Invalid temporal validity bound (expected ISO-8601 date or timestamp): {exc}") from exc
    if norm_vf or norm_vu:
        tv = evaluate_temporal_validity(norm_vf, norm_vu)
        if tv["status"] == "invalid_window":
            raise ReviewTransitionError(
                f"Invalid temporal validity window: valid_from ({norm_vf}) must not be after valid_until ({norm_vu})."
            )
    return norm_vf, norm_vu


class ReviewTransitionError(RuntimeError):
    """The requested mutation does not satisfy the required integrity gates."""


@contextmanager
def review_lock(lock_path: str):
    """Use one OS advisory lock for all local canonical and derived-state writers."""
    try:
        fh = open(lock_path, "r+b")
    except FileNotFoundError:
        directory = os.path.dirname(os.path.abspath(lock_path))
        os.makedirs(directory, exist_ok=True)
        with open(lock_path, "a+b") as init_fh:
            init_fh.seek(0)
            if not init_fh.read(1):
                init_fh.seek(0)
                init_fh.write(b"0")
                init_fh.flush()
        fh = open(lock_path, "r+b")
    with fh as lock_file:
        if os.fstat(lock_file.fileno()).st_size < 1:
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


def decision_log_path(brain_dir: str) -> str:
    """Return canonical append-only owner decision log path under brain_dir."""
    return os.path.join(os.path.abspath(brain_dir), "0-raw", "archive", "decision-record.jsonl")


_DECISION_CACHE_LOCK = threading.Lock()
# Maps abs_log_path -> (mtime_ns, ctime_ns, size_bytes, prefix_sha256, latest_by_atom_filename_dict)
_DECISION_LOG_CACHE: dict[str, tuple[int, int, int, str, dict[str, dict]]] = {}
# Maps atom_sha256 -> (valid_approved_frontmatter_bool, anchors_dict, clean_src_rel, recorded_src_sha, os_src_rel, sources_tuple, evidence_excerpt)
_ATOM_ANCHOR_PARSE_CACHE: dict[
    str,
    tuple[bool, dict[str, object], str, str, str, tuple[str, ...], str],
] = {}
# Maps (norm_brain_dir, atom_sha256, src_sha256) -> anchor_verified_bool
_ANCHOR_VERIFY_BY_HASH_CACHE: dict[tuple[str, str, str], bool] = {}
# Maps normcase(abs_src_path) -> (mtime_ns, ctime_ns, size, src_sha256) for large (>64) vaults
_SOURCE_STAT_SHA_CACHE: dict[str, tuple[int, int, int, str]] = {}


def invalidate_decision_log_cache(decision_log_path: str | None = None) -> None:
    with _DECISION_CACHE_LOCK:
        if decision_log_path is None:
            _DECISION_LOG_CACHE.clear()
            _ATOM_ANCHOR_PARSE_CACHE.clear()
            _ANCHOR_VERIFY_BY_HASH_CACHE.clear()
            _SOURCE_STAT_SHA_CACHE.clear()
        else:
            _DECISION_LOG_CACHE.pop(os.path.abspath(decision_log_path), None)


def latest_decisions_by_atom(decision_log_path: str) -> dict[str, dict]:
    """Return latest decision record per atom_filename in O(1) cached time when log is unchanged."""
    abs_log = os.path.abspath(decision_log_path)
    if _is_reparse_or_link(os.path.dirname(abs_log)):
        raise PermissionError(f"Refusing symlink or reparse-point decision log parent: {abs_log}")
    st = _lstat_regular_non_reparse(abs_log)
    if st is None:
        if not os.path.lexists(abs_log):
            raise FileNotFoundError(abs_log)
        raise PermissionError(f"Refusing symlink or non-regular decision log: {abs_log}")
    with _DECISION_CACHE_LOCK:
        cached = _DECISION_LOG_CACHE.get(abs_log)
    if (
        cached is not None
        and len(cached[4]) > LARGE_VAULT_STAT_CACHE_THRESHOLD
        and cached[0] == st.st_mtime_ns
        and cached[1] == st.st_ctime_ns
        and cached[2] == st.st_size
    ):
        return cached[4]
    with open(abs_log, "rb") as raw_fh:
        raw = raw_fh.read()
    # Hashing raw bytes is cheap (GB/s); JSON parsing is the expensive part we avoid.
    full_sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    if cached is not None and cached[2] == size and cached[3] == full_sha:
        return cached[4]

    # Parse only the appended tail when the cached prefix is byte-identical; else full scan.
    latest_map: dict[str, dict] = {}
    start_offset = 0
    if (
        cached is not None
        and size > cached[2]
        and raw[cached[2] - 1 : cached[2]] == b"\n"
        and hashlib.sha256(raw[: cached[2]]).hexdigest() == cached[3]
    ):
        start_offset = cached[2]
        latest_map = dict(cached[4])
    tail_text = raw[start_offset:].decode("utf-8")

    for line in tail_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed_line = json.loads(stripped)
        if not isinstance(parsed_line, dict):
            raise TypeError("Decision log record must be a JSON object.")
        fname = parsed_line.get("atom_filename")
        if isinstance(fname, str) and fname:
            latest_map[fname] = parsed_line

    with _DECISION_CACHE_LOCK:
        if len(_DECISION_LOG_CACHE) > 256:
            _DECISION_LOG_CACHE.clear()
        _DECISION_LOG_CACHE[abs_log] = (st.st_mtime_ns, st.st_ctime_ns, size, full_sha, latest_map)
    return latest_map


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
    if str(path).endswith(".md"):
        record_known_atom_hash(path, content_sha256(content))


def append_decision_record(record: dict, decision_log_path: str):
    abs_log = os.path.abspath(decision_log_path)
    directory = os.path.dirname(abs_log)
    os.makedirs(directory, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(abs_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("Decision record append was incomplete.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    invalidate_decision_log_cache(abs_log)


def append_decision_records(records: list[dict], decision_log_path: str):
    """Append a related group of audit records in one O_APPEND write."""
    abs_log = os.path.abspath(decision_log_path)
    directory = os.path.dirname(abs_log)
    os.makedirs(directory, exist_ok=True)
    payload = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records).encode("utf-8")
    descriptor = os.open(abs_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("Decision record group append was incomplete.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    invalidate_decision_log_cache(abs_log)


def refresh_lexical_index(
    atoms_dir: str,
    brain_dir: str,
    *,
    changed_filenames: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Refresh the rebuildable model-free index while the shared writer lock is held."""
    from scripts.lexical_store import rebuild_lexical_index, upsert_lexical_atoms

    index_path = os.path.join(brain_dir, ".sidecar", "lexical.sqlite3")
    if changed_filenames:
        return upsert_lexical_atoms(atoms_dir, index_path, changed_filenames)
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
    extra_record_fields: dict | None = None,
) -> dict:
    before_hash = content_sha256(original)
    after_hash = content_sha256(updated)
    os.makedirs(history_dir, exist_ok=True)
    backup_path = os.path.join(history_dir, f"{Path(atom_path).name}.{before_hash}.bak")
    if not os.path.exists(backup_path):
        atomic_write_bytes(backup_path, original)

    atomic_write_bytes(atom_path, updated)
    try:
        index_result = refresh_lexical_index(
            os.path.dirname(atom_path),
            brain_dir,
            changed_filenames=[Path(atom_path).name],
        )
        dense_state = "not_active"
        dense_generation = None
        dense_error = None
        if dense_refresh_required:
            try:
                import sys as _sys

                _rv_mod = _sys.modules.get("rebuild_vectors")
                if _rv_mod is None:
                    import scripts.rebuild_vectors as _rv_mod

                upsert_fn = getattr(_rv_mod, "upsert_changed_vectors", None)
                if callable(upsert_fn):
                    dense_result = upsert_fn(
                        atoms_dir=os.path.dirname(atom_path),
                        store_path=os.path.join(brain_dir, ".chroma"),
                        collection_name="atoms",
                        model=dense_model,
                        changed_filenames=(Path(atom_path).name,),
                        pre_verified_current=True,
                        _writer_lock_held=True,
                    )
                else:
                    dense_result = _rv_mod.rebuild_vectors(
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
            "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds"),
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
        if extra_record_fields:
            record.update(extra_record_fields)
        append_decision_record(record, decision_log_path)
    except Exception:
        atomic_write_bytes(atom_path, original)
        try:
            refresh_lexical_index(os.path.dirname(atom_path), brain_dir)
        except Exception:
            pass
        raise
    return record


def _recorded_attestation(actor: str, attestation: dict | None) -> tuple[str, dict]:
    """Reject synthetic seed actors. Verify a credential when the caller presents one."""
    from scripts.owner_attestation import OwnerAttestationError, require_attestation, unattested_library_attestation

    try:
        if attestation is None:
            return unattested_library_attestation(actor)
        recorded = require_attestation(
            attestation,
            actor=actor,
            state_dir=attestation.get("state_dir"),
            auth_path=attestation.get("auth_path"),
        )
        return recorded["actor"], recorded
    except OwnerAttestationError as exc:
        raise ReviewTransitionError(str(exc)) from exc


def apply_atom_mutation(
    *,
    action: str,
    atom_path: str,
    expected_sha256: str,
    actor: str,
    edited_content: str | None = None,
    decision_log_path: str | None = None,
    history_dir: str | None = None,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    reviewed_on: str | None = None,
    attestation: dict | None = None,
) -> dict:
    """Compare, mutate, reindex, and attest one atom revision as one serialized operation."""
    from scripts.config import resolve_module_atoms_and_brain

    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    atoms_dir = resolved_atoms
    brain_dir = resolved_brain
    atom_path = _canonical_atom_path(atom_path, atoms_dir)
    decision_log_path = (
        os.path.abspath(decision_log_path)
        if decision_log_path is not None
        else os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    )
    history_dir = os.path.abspath(history_dir or os.path.join(atoms_dir, ".history"))

    actor, recorded_attestation = _recorded_attestation(actor, attestation)
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
        current_values = parse_frontmatter_values(frontmatter)
        current_superseded_by = current_values.get("superseded_by", [])
        if isinstance(current_superseded_by, str):
            current_superseded_by = [current_superseded_by] if current_superseded_by.strip() else []
        if (
            current_values.get("review_status") == "merged"
            or current_values.get("claim_state") == "superseded"
            or bool(current_superseded_by)
        ):
            raise ReviewTransitionError(
                "Merged or superseded atoms cannot be approved, edited, or demoted; choose and review the surviving atom instead."
            )

        from scripts.index_manifest import (
            INDEX_SCHEMA_VERSION,
            atom_revisions,
            index_manifest_is_current,
            load_index_manifest,
            revisions_fingerprint,
        )

        active_dense_manifest = load_index_manifest(os.path.join(brain_dir, ".chroma"))
        manifest_valid = (
            isinstance(active_dense_manifest, dict)
            and active_dense_manifest.get("schema_version") == INDEX_SCHEMA_VERSION
            and active_dense_manifest.get("distance_metric") == "cosine"
            and isinstance(active_dense_manifest.get("embedding_model"), str)
            and bool(active_dense_manifest.get("embedding_model"))
            and isinstance(active_dense_manifest.get("embedding_dimensions"), int)
            and bool(active_dense_manifest.get("generation_id"))
        )
        dense_refresh_required = False
        if manifest_valid and isinstance(active_dense_manifest, dict):
            if index_manifest_is_current(active_dense_manifest, atoms_dir):
                dense_refresh_required = True
            else:
                # Also handle newly-created atoms (e.g. verify_note_excerpt / new draft approval)
                # where all previously-indexed atoms are untouched and only this atom was added.
                target_name = Path(atom_path).name
                other_revs = [r for r in atom_revisions(atoms_dir) if r["filename"] != target_name]
                indexed = active_dense_manifest.get("indexed_count")
                skipped = active_dense_manifest.get("skipped_count", 0)
                if (
                    isinstance(indexed, int)
                    and isinstance(skipped, int)
                    and skipped >= 0
                    and active_dense_manifest.get("atom_count") == len(other_revs)
                    and (indexed + skipped) == len(other_revs)
                    and active_dense_manifest.get("atom_revisions_sha256") == revisions_fingerprint(other_revs)
                ):
                    dense_refresh_required = True

        evidence_anchor = None
        if action == "edit":
            try:
                edited_text = str(edited_content).encode("utf-8").decode("utf-8")
            except UnicodeEncodeError as exc:
                raise ReviewTransitionError("Atom content must be valid UTF-8 Markdown.") from exc
            edit_parsed = split_frontmatter(edited_text)
            if edit_parsed is None or not is_memory_document(edited_text):
                raise ReviewTransitionError("Atom edits must retain valid frontmatter, Claim, and Evidence sections.")
            _validate_frontmatter_temporal_bounds(edit_parsed[0])
            source_origin = str(current_values.get("origin", "imported_unknown"))
            updated_text = normalize_memory_document(
                edited_text,
                source_filename=Path(atom_path).name,
                current_date=datetime.datetime.now().strftime("%Y-%m-%d"),
                origin=source_origin,
            )
            norm_parsed = split_frontmatter(updated_text)
            if norm_parsed is not None:
                norm_fm, norm_body = norm_parsed
                existing_supersedes = current_values.get("supersedes", [])
                if isinstance(existing_supersedes, str):
                    existing_supersedes = [existing_supersedes] if existing_supersedes.strip() else []
                norm_fm = set_string_list_field(norm_fm, "supersedes", list(existing_supersedes or []), "contexts")
                norm_fm = set_string_list_field(norm_fm, "superseded_by", list(current_superseded_by or []), "supersedes")
                updated_text = render_memory_document(norm_fm, norm_body)
            updated = updated_text.encode("utf-8")
        else:
            frontmatter = remove_frontmatter_fields(frontmatter, {"epistemic_contract"})
            if action == "approve":
                _validate_frontmatter_temporal_bounds(frontmatter)
                epistemic_label = frontmatter_value(frontmatter, "epistemic_label")
                if epistemic_label == "hypothesis":
                    raise ReviewTransitionError("Hypotheses cannot be promoted to factual standard recall.")
                if epistemic_label == "preference":
                    frontmatter = set_scalar_field(
                        frontmatter, "epistemic_contract", "owner_subjective_preference", "epistemic_label"
                    )
                try:
                    evidence_anchor = find_evidence_anchor(original_text, brain_dir)
                except (EvidenceValidationError, OSError, ValueError) as exc:
                    raise ReviewTransitionError(f"Approval requires verifiable source evidence: {exc}") from exc
                if epistemic_label != "preference":
                    claim_text = section_text("\n".join(body), "Claim")
                    evidence_text = section_text("\n".join(body), "Evidence")
                    if not claim_entailed_by_evidence(claim_text, evidence_text):
                        raise ReviewTransitionError(
                            "Approval requires the claim to be supported by its own evidence quote."
                        )

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
            extra_record_fields={"attestation": recorded_attestation},
        )


def apply_atom_edit(**kwargs) -> dict:
    """Compatibility wrapper; all behavior lives in apply_atom_mutation."""
    return apply_atom_mutation(action="edit", **kwargs)


def apply_review_transition(*, action: str, **kwargs) -> dict:
    """Compatibility wrapper; all behavior lives in apply_atom_mutation."""
    return apply_atom_mutation(action=action, **kwargs)


_DISALLOWED_TEMPORAL_ACTORS = {
    "",
    "agent",
    "ai",
    "assistant",
    "bot",
    "claude",
    "cursor",
    "gemini",
    "gpt",
    "llm",
    "mcp",
    "model",
    "ollama",
    "system",
}


def validate_actor_name(actor: str) -> str:
    """Validate and normalize an explicit human owner actor name."""
    clean_actor = str(actor or "").strip()[:120]
    actor_tokens = {tok for tok in __import__("re").split(r"[^a-z0-9]+", clean_actor.casefold()) if tok}
    if not clean_actor or bool(actor_tokens & _DISALLOWED_TEMPORAL_ACTORS):
        raise ReviewTransitionError("This owner-gated operation requires an explicit human owner actor.")
    return clean_actor


def vault_writer_lock(brain_dir: str):
    """Acquire the canonical sidecar writer.lock for a vault root."""
    return review_lock(os.path.join(str(brain_dir), ".sidecar", "writer.lock"))


def set_atom_temporal_bounds(
    atom_path: str,
    *,
    valid_from: str | None = None,
    valid_until: str | None = None,
    expected_sha256: str,
    actor: str,
    rationale: str = "",
    decision_log_path: str | None = None,
    history_dir: str | None = None,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    attestation: dict | None = None,
) -> dict:
    """Set or clear optional ``valid_from`` / ``valid_until`` bounds on an atom under ``writer.lock``.

    Preserves owner approval currency on already-approved atoms by re-verifying source byte-offset
    anchors and recording an attested owner approval; never promotes unapproved/pending atoms.
    """
    from scripts.config import resolve_module_atoms_and_brain

    brain_dir, atoms_dir = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    atom_path = _canonical_atom_path(atom_path, atoms_dir)
    decision_log_path = (
        os.path.abspath(decision_log_path)
        if decision_log_path is not None
        else os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    )
    history_dir = os.path.abspath(history_dir or os.path.join(atoms_dir, ".history"))

    clean_actor, recorded_attestation = _recorded_attestation(actor, attestation)
    actor_tokens = {tok for tok in __import__("re").split(r"[^a-z0-9]+", clean_actor.casefold()) if tok}
    if bool(actor_tokens & _DISALLOWED_TEMPORAL_ACTORS):
        raise ReviewTransitionError("Setting temporal validity bounds requires an explicit human owner actor.")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ReviewConflictError("An exact 64-character expected SHA-256 is required.")

    try:
        norm_vf = normalize_temporal_bound_str(valid_from) if valid_from else ""
        norm_vu = normalize_temporal_bound_str(valid_until) if valid_until else ""
    except ValueError as exc:
        raise ReviewTransitionError(f"Invalid temporal validity bound: {exc}") from exc
    if norm_vf or norm_vu:
        tv = evaluate_temporal_validity(norm_vf, norm_vu)
        if tv["status"] == "invalid_window":
            raise ReviewTransitionError(
                f"Invalid temporal validity window: valid_from ({norm_vf}) must not be after valid_until ({norm_vu})."
            )

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    with review_lock(lock_path):
        original = Path(atom_path).read_bytes()
        actual_hash = content_sha256(original)
        if actual_hash != expected_sha256:
            raise ReviewConflictError(
                f"Atom revision changed (expected {expected_sha256}, found {actual_hash}); reopen it first."
            )
        original_text = original.decode("utf-8")
        parsed = split_frontmatter(original_text)
        if parsed is None or not is_memory_document(original_text):
            raise ReviewTransitionError("Atom must contain valid frontmatter, Claim, and Evidence sections.")
        frontmatter, body = parsed
        values = parse_frontmatter_values(frontmatter)
        sup_by = values.get("superseded_by", [])
        if isinstance(sup_by, str):
            sup_by = [sup_by] if sup_by.strip() else []
        if values.get("review_status") == "merged" or values.get("claim_state") == "superseded" or bool(sup_by):
            raise ReviewTransitionError("Merged or superseded atoms cannot be mutated.")

        was_approved = approved_revision_is_current(
            atom_path,
            decision_log_path=decision_log_path,
            brain_dir=brain_dir,
            atoms_dir=atoms_dir,
        )

        updated_fm = list(frontmatter)
        if norm_vf:
            updated_fm = set_scalar_field(updated_fm, "valid_from", norm_vf, "last-reviewed")
        else:
            updated_fm = remove_frontmatter_fields(updated_fm, {"valid_from"})
        if norm_vu:
            updated_fm = set_scalar_field(
                updated_fm,
                "valid_until",
                norm_vu,
                "valid_from" if norm_vf else "last-reviewed",
            )
        else:
            updated_fm = remove_frontmatter_fields(updated_fm, {"valid_until"})

        evidence_anchor = None
        recorded_action = "edit"
        if was_approved:
            candidate_doc = render_memory_document(updated_fm, body)
            try:
                evidence_anchor = find_evidence_anchor(candidate_doc, brain_dir)
            except (EvidenceValidationError, OSError, ValueError) as exc:
                raise ReviewTransitionError(f"Could not verify evidence anchor while setting temporal bounds: {exc}") from exc
            updated_fm = set_scalar_field(updated_fm, "citation_integrity", "anchored", "origin")
            for key in ANCHOR_FIELDS:
                updated_fm = set_scalar_field(updated_fm, key, str(evidence_anchor[key]), "citation_integrity")
            recorded_action = "approve"
        else:
            updated_fm = set_scalar_field(updated_fm, "review_status", "pending", "contexts")
            updated_fm = set_scalar_field(updated_fm, "claim_state", "candidate", "review_status")

        updated_bytes = render_memory_document(updated_fm, body).encode("utf-8")

        from scripts.index_manifest import INDEX_SCHEMA_VERSION, index_manifest_is_current, load_index_manifest

        active_dense_manifest = load_index_manifest(os.path.join(brain_dir, ".chroma"))
        dense_refresh_required = (
            isinstance(active_dense_manifest, dict)
            and active_dense_manifest.get("schema_version") == INDEX_SCHEMA_VERSION
            and active_dense_manifest.get("distance_metric") == "cosine"
            and isinstance(active_dense_manifest.get("embedding_model"), str)
            and bool(active_dense_manifest.get("embedding_model"))
            and isinstance(active_dense_manifest.get("embedding_dimensions"), int)
            and bool(active_dense_manifest.get("generation_id"))
            and index_manifest_is_current(active_dense_manifest, atoms_dir)
        )

        clean_rationale = str(rationale or "").strip()[:500]
        res = _write_mutation(
            atom_path=atom_path,
            original=original,
            updated=updated_bytes,
            action=recorded_action,
            actor=clean_actor,
            decision_log_path=decision_log_path,
            history_dir=history_dir,
            brain_dir=brain_dir,
            evidence_anchor=evidence_anchor,
            dense_refresh_required=dense_refresh_required,
            dense_model=active_dense_manifest.get("embedding_model") if dense_refresh_required else None,
            extra_record_fields={
                "valid_from": norm_vf,
                "valid_until": norm_vu,
                "rationale": clean_rationale,
                "attestation": recorded_attestation,
            },
        )
        tv_now = evaluate_temporal_validity(norm_vf, norm_vu)
        return {
            **res,
            "approved_current": bool(was_approved),
            "valid_from": norm_vf,
            "valid_until": norm_vu,
            "temporal_status": tv_now["status"],
            "rationale": clean_rationale,
        }


def archive_atom_to_cold(
    atom_path: str,
    *,
    expected_sha256: str,
    actor: str,
    reason: str = "",
    decision_log_path: str | None = None,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
) -> dict:
    """Reversibly move a low-gravity atom from ``2-atoms/`` to ``2-atoms/.cold-archive/`` under Owner CAS."""
    from scripts.config import resolve_module_atoms_and_brain

    brain_dir, atoms_dir = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    atom_path = _canonical_atom_path(atom_path, atoms_dir)
    decision_log_path = (
        os.path.abspath(decision_log_path)
        if decision_log_path is not None
        else os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    )
    clean_actor = str(actor or "").strip()[:120]
    actor_tokens = {tok for tok in __import__("re").split(r"[^a-z0-9]+", clean_actor.casefold()) if tok}
    if not clean_actor or bool(actor_tokens & _DISALLOWED_TEMPORAL_ACTORS):
        raise ReviewTransitionError("Cold-archiving an atom requires an explicit human owner actor.")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ReviewConflictError("An exact 64-character expected SHA-256 is required.")

    cold_dir = os.path.join(atoms_dir, ".cold-archive")
    if os.path.lexists(cold_dir) and _is_reparse_or_link(cold_dir):
        raise PermissionError(f"Refusing symlinked cold-archive dir: {cold_dir}")
    os.makedirs(cold_dir, exist_ok=True)
    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    fname = os.path.basename(atom_path)
    cold_path = os.path.join(cold_dir, fname)

    with review_lock(lock_path):
        original = Path(atom_path).read_bytes()
        actual_hash = content_sha256(original)
        if actual_hash != expected_sha256:
            raise ReviewConflictError(f"Atom revision changed (expected {expected_sha256}, found {actual_hash}).")
        atomic_write_bytes(cold_path, original)
        os.unlink(atom_path)
        ts = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        rec = {
            "atom_filename": fname,
            "action": "cold_archive",
            "actor": clean_actor,
            "before_sha256": actual_hash,
            "after_sha256": actual_hash,
            "cold_archive_path": f"2-atoms/.cold-archive/{fname}",
            "reason": str(reason or "").strip()[:500],
            "timestamp": ts,
        }
        append_decision_records([rec], decision_log_path)
        refresh_lexical_index(atoms_dir, brain_dir)

    return {
        "status": "cold_archived",
        "filename": fname,
        "cold_path": cold_path,
        "sha256": actual_hash,
        "timestamp": ts,
    }


def restore_cold_archived_atom(
    cold_atom_path_or_name: str,
    *,
    expected_sha256: str,
    actor: str,
    reason: str = "",
    decision_log_path: str | None = None,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
) -> dict:
    """Restore a cold-archived atom from ``2-atoms/.cold-archive/`` back into ``2-atoms/`` under Owner CAS."""
    from scripts.config import resolve_module_atoms_and_brain, safe_file_in_dir

    brain_dir, atoms_dir = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    decision_log_path = (
        os.path.abspath(decision_log_path)
        if decision_log_path is not None
        else os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    )
    clean_actor = str(actor or "").strip()[:120]
    actor_tokens = {tok for tok in __import__("re").split(r"[^a-z0-9]+", clean_actor.casefold()) if tok}
    if not clean_actor or bool(actor_tokens & _DISALLOWED_TEMPORAL_ACTORS):
        raise ReviewTransitionError("Restoring a cold-archived atom requires an explicit human owner actor.")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ReviewConflictError("An exact 64-character expected SHA-256 is required.")

    cold_dir = os.path.join(atoms_dir, ".cold-archive")
    fname = os.path.basename(str(cold_atom_path_or_name or "").strip())
    if not fname or not fname.endswith(".md"):
        raise ReviewTransitionError(f"Invalid cold-archive atom name: {cold_atom_path_or_name!r}")
    cold_path = safe_file_in_dir(cold_dir, fname, allowed_exts={".md"}, must_exist=True)
    if not cold_path or not os.path.isfile(cold_path) or _is_reparse_or_link(cold_path):
        raise FileNotFoundError(f"Cold-archived atom not found: {fname}")

    target_path = os.path.join(atoms_dir, fname)
    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    with review_lock(lock_path):
        if os.path.exists(target_path):
            raise ReviewConflictError(f"Cannot restore {fname}: active file already exists at {target_path}.")
        archived = Path(cold_path).read_bytes()
        actual_hash = content_sha256(archived)
        if actual_hash != expected_sha256:
            raise ReviewConflictError(f"Cold archive revision changed (expected {expected_sha256}, found {actual_hash}).")

        text = archived.decode("utf-8")
        parsed = split_frontmatter(text)
        fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
        was_accepted = str(fm_vals.get("review_status", "")).strip() == "accepted" and str(fm_vals.get("claim_state", "")).strip() == "human_reviewed"
        anchor = None
        if was_accepted:
            anchor = find_evidence_anchor(text, brain_dir)
            if parsed is not None:
                upd_fm = set_scalar_field(parsed[0], "citation_integrity", "anchored", "origin")
                for k in ANCHOR_FIELDS:
                    upd_fm = set_scalar_field(upd_fm, k, str(anchor[k]), "citation_integrity")
                restored_bytes = render_memory_document(upd_fm, parsed[1]).encode("utf-8")
            else:
                restored_bytes = archived
        else:
            restored_bytes = archived

        after_hash = content_sha256(restored_bytes)
        atomic_write_bytes(target_path, restored_bytes)
        os.unlink(cold_path)
        ts = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        rec: dict = {
            "schema_version": 1,
            "recorded_at": ts,
            "atom_filename": fname,
            "action": "approve" if was_accepted else "restore_cold_archive",
            "actor": clean_actor,
            "content_sha256_before": actual_hash,
            "content_sha256_after": after_hash,
            "restored_from_cold_archive": True,
            "reason": str(reason or "").strip()[:500],
            "timestamp": ts,
        }
        if anchor is not None:
            rec.update(anchor)
        append_decision_records([rec], decision_log_path)
        invalidate_decision_log_cache()
        refresh_lexical_index(atoms_dir, brain_dir)

    return {
        "status": "restored",
        "filename": fname,
        "path": target_path,
        "sha256": after_hash,
        "approved_current": bool(was_accepted),
        "timestamp": ts,
    }


def apply_atom_merge(
    *, survivor_path: str, survivor_expected_sha256: str,
    superseded_path: str, superseded_expected_sha256: str,
    actor: str, rationale: str, decision_log_path: str | None = None,
    history_dir: str | None = None, brain_dir: str | None = None,
    atoms_dir: str | None = None, reviewed_on: str | None = None,
    attestation: dict | None = None,
) -> list[dict]:
    """Owner-directed two-atom supersession with CAS, rollback, and paired audit."""
    from scripts.config import resolve_module_atoms_and_brain

    brain_dir, atoms_dir = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    survivor_path = _canonical_atom_path(survivor_path, atoms_dir)
    superseded_path = _canonical_atom_path(superseded_path, atoms_dir)
    decision_log_path = (
        os.path.abspath(decision_log_path)
        if decision_log_path is not None
        else os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    )
    history_dir = os.path.abspath(history_dir or os.path.join(atoms_dir, ".history"))
    if survivor_path == superseded_path:
        raise ReviewTransitionError("An atom cannot supersede itself.")
    actor, recorded_attestation = _recorded_attestation(actor, attestation)
    if not isinstance(rationale, str) or not rationale.strip():
        raise ReviewTransitionError("A non-empty owner rationale is required for atom merges.")
    for value, label in ((survivor_expected_sha256, "survivor"), (superseded_expected_sha256, "superseded")):
        if not isinstance(value, str) or len(value) != 64:
            raise ReviewConflictError(f"An exact 64-character expected SHA-256 is required for the {label} atom.")

    lock_path = os.path.join(brain_dir, ".sidecar", "writer.lock")
    with review_lock(lock_path):
        paths = [survivor_path, superseded_path]
        originals = [Path(path).read_bytes() for path in paths]
        expected = [survivor_expected_sha256, superseded_expected_sha256]
        actual = [content_sha256(data) for data in originals]
        for label, want, got in zip(("survivor", "superseded"), expected, actual):
            if want != got:
                raise ReviewConflictError(f"The {label} atom changed (expected {want}, found {got}); reopen both atoms before merging.")

        parsed_docs = []
        for label, data in zip(("survivor", "superseded"), originals):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReviewTransitionError(f"The {label} atom is not valid UTF-8 Markdown.") from exc
            parsed = split_frontmatter(text)
            if parsed is None or not is_memory_document(text):
                raise ReviewTransitionError(f"The {label} atom must contain valid frontmatter, Claim, and Evidence sections.")
            frontmatter, body = parsed
            from scripts.memory_review import parse_frontmatter_values
            values = parse_frontmatter_values(frontmatter)
            existing_sup_by = values.get("superseded_by", [])
            if isinstance(existing_sup_by, str):
                existing_sup_by = [existing_sup_by] if existing_sup_by.strip() else []
            if values.get("review_status") == "merged" or values.get("claim_state") == "superseded" or bool(existing_sup_by):
                raise ReviewTransitionError(f"The {label} atom is already superseded.")
            parsed_docs.append((frontmatter, body, values))

        survivor_frontmatter, survivor_body, survivor_values = parsed_docs[0]
        superseded_frontmatter, superseded_body, superseded_values = parsed_docs[1]
        survivor_links = survivor_values.get("supersedes", [])
        superseded_links = superseded_values.get("superseded_by", [])
        superseded_supersedes = superseded_values.get("supersedes", [])
        if isinstance(survivor_links, str):
            survivor_links = [survivor_links]
        if isinstance(superseded_links, str):
            superseded_links = [superseded_links]
        if isinstance(superseded_supersedes, str):
            superseded_supersedes = [superseded_supersedes]
        survivor_links = list(survivor_links or [])
        superseded_links = list(superseded_links or [])
        superseded_supersedes = list(superseded_supersedes or [])
        survivor_name = Path(survivor_path).name
        superseded_name = Path(superseded_path).name
        if superseded_name in survivor_links or survivor_name in superseded_links:
            raise ReviewTransitionError("These atoms already have a merge relationship.")
        if survivor_name in superseded_supersedes:
            raise ReviewTransitionError("Merge would create a supersession cycle.")

        # Full transitive cycle check across supersedes lineage
        visited: set[str] = set()
        queue = list(superseded_supersedes)
        while queue:
            current_name = str(queue.pop(0))
            if current_name == survivor_name:
                raise ReviewTransitionError("Merge would create a supersession cycle.")
            if current_name in visited:
                continue
            visited.add(current_name)
            related_path = os.path.join(atoms_dir, current_name)
            if not os.path.isfile(related_path):
                continue
            try:
                related_doc = Path(related_path).read_text(encoding="utf-8")
                related_parsed = split_frontmatter(related_doc)
                related_frontmatter = related_parsed[0] if related_parsed else []
                related_values = parse_frontmatter_values(related_frontmatter)
            except (OSError, UnicodeDecodeError):
                continue
            next_supersedes = related_values.get("supersedes", [])
            if isinstance(next_supersedes, str):
                next_supersedes = [next_supersedes]
            queue.extend(str(item) for item in (next_supersedes or []))
        survivor_links.append(Path(superseded_path).name)
        superseded_links.append(Path(survivor_path).name)
        reviewed_on = reviewed_on or datetime.datetime.now().strftime("%Y-%m-%d")
        updated_survivor = set_string_list_field(survivor_frontmatter, "supersedes", survivor_links, "contexts")
        updated_survivor = set_scalar_field(updated_survivor, "claim_state", "candidate", "citation_integrity")
        updated_survivor = set_scalar_field(updated_survivor, "review_status", "pending", "epistemic_label")
        updated_survivor = set_scalar_field(updated_survivor, "last-reviewed", reviewed_on, "created")
        updated_superseded = set_string_list_field(superseded_frontmatter, "superseded_by", superseded_links, "supersedes")
        updated_superseded = set_scalar_field(updated_superseded, "claim_state", "superseded", "citation_integrity")
        updated_superseded = set_scalar_field(updated_superseded, "review_status", "merged", "epistemic_label")
        updated_superseded = set_scalar_field(updated_superseded, "last-reviewed", reviewed_on, "created")
        updated = [
            render_memory_document(updated_survivor, survivor_body).encode("utf-8"),
            render_memory_document(updated_superseded, superseded_body).encode("utf-8"),
        ]

        from scripts.index_manifest import index_manifest_is_current, load_index_manifest

        dense_manifest = load_index_manifest(os.path.join(brain_dir, ".chroma"))
        dense_active = (
            isinstance(dense_manifest, dict)
            and dense_manifest.get("distance_metric") == "cosine"
            and bool(dense_manifest.get("embedding_model"))
            and isinstance(dense_manifest.get("embedding_dimensions"), int)
            and bool(dense_manifest.get("generation_id"))
            and index_manifest_is_current(dense_manifest, atoms_dir)
        )

        os.makedirs(history_dir, exist_ok=True)
        backup_paths = []
        for path, original in zip(paths, originals):
            backup_path = os.path.join(history_dir, f"{Path(path).name}.{content_sha256(original)}.bak")
            if not os.path.exists(backup_path):
                atomic_write_bytes(backup_path, original)
            backup_paths.append(backup_path)
        written_paths = []
        try:
            for path, content in zip(paths, updated):
                atomic_write_bytes(path, content)
                written_paths.append(path)
            index_result = refresh_lexical_index(
                atoms_dir,
                brain_dir,
                changed_filenames=[survivor_name, superseded_name],
            )
            merge_dense_state = "not_active"
            merge_dense_gen = None
            merge_dense_error = None
            if dense_active:
                try:
                    import sys as _sys

                    _rv_mod = _sys.modules.get("rebuild_vectors")
                    if _rv_mod is None:
                        import scripts.rebuild_vectors as _rv_mod
                    upsert_fn = getattr(_rv_mod, "upsert_changed_vectors", None)
                    if callable(upsert_fn):
                        dense_res = upsert_fn(
                            atoms_dir=atoms_dir,
                            store_path=os.path.join(brain_dir, ".chroma"),
                            collection_name="atoms",
                            model=str(dense_manifest.get("embedding_model")),
                            changed_filenames=(survivor_name, superseded_name),
                            pre_verified_current=True,
                            _writer_lock_held=True,
                        )
                    else:
                        dense_res = _rv_mod.rebuild_vectors(
                            atoms_dir=atoms_dir,
                            store_path=os.path.join(brain_dir, ".chroma"),
                            collection_name="atoms",
                            model=str(dense_manifest.get("embedding_model")),
                            _writer_lock_held=True,
                        )
                    merge_dense_state = "current" if dense_res.get("committed") else "stale"
                    merge_dense_gen = dense_res.get("generation_id") if merge_dense_state == "current" else None
                except Exception as exc:
                    merge_dense_state = "stale"
                    merge_dense_error = str(exc)[:1000]
            now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
            peer_paths = [superseded_path, survivor_path]
            records = []
            for index, path in enumerate(paths):
                rec_entry = {
                    "schema_version": 1,
                    "recorded_at": now,
                    "action": "merge",
                    "actor": actor.strip(),
                    "rationale": rationale.strip(),
                    "attestation": recorded_attestation,
                    "atom_filename": Path(path).name,
                    "peer_filename": Path(peer_paths[index]).name,
                    "relationship": "survivor" if index == 0 else "superseded",
                    "content_sha256_before": content_sha256(originals[index]),
                    "content_sha256_after": content_sha256(updated[index]),
                    "backup_path": backup_paths[index],
                    "lexical_index_path": index_result["index_path"],
                    "dense_index_state": merge_dense_state,
                }
                if merge_dense_gen:
                    rec_entry["dense_index_generation"] = merge_dense_gen
                if merge_dense_error:
                    rec_entry["dense_index_error"] = merge_dense_error
                records.append(rec_entry)
            append_decision_records(records, decision_log_path)
        except Exception:
            for path, original in zip(paths, originals):
                if path in written_paths:
                    atomic_write_bytes(path, original)
            try:
                refresh_lexical_index(atoms_dir, brain_dir)
            except Exception:
                pass
            raise
        return records


def _lstat_regular_non_reparse(path: str) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return None
    if os.name == "nt":
        if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return None
        return st
    return st


def approved_revision_is_current(
    atom_path: str,
    decision_log_path: str | None = None,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    *,
    known_atom_bytes: bytes | None = None,
    known_atom_sha256: str | None = None,
    known_decision_map: dict[str, dict] | None = None,
    source_sha_memo: dict[str, str | None] | None = None,
    canonical_atoms_root: str | None = None,
    known_atom_filename: str | None = None,
    known_brain_abs: str | None = None,
    known_norm_brain_abs: str | None = None,
) -> bool:
    """Accept only the latest owner approval for the exact atom and evidence revisions."""
    content = ""
    anchors: dict[str, object] = {}
    current_hash = ""
    try:
        has_pre_resolved_brain = (
            canonical_atoms_root is not None
            and known_atom_bytes is not None
            and known_brain_abs is not None
            and known_norm_brain_abs is not None
        )
        fast_path = has_pre_resolved_brain or (
            canonical_atoms_root is not None
            and known_atom_bytes is not None
            and brain_dir is not None
            and os.path.isabs(brain_dir)
        )
        if not fast_path:
            from scripts.config import resolve_module_atoms_and_brain

            brain_dir, atoms_dir = resolve_module_atoms_and_brain(
                globals(),
                atoms_dir=atoms_dir,
                brain_dir=brain_dir,
            )
        elif atoms_dir is None:
            atoms_dir = canonical_atoms_root
        if has_pre_resolved_brain:
            brain_dir = known_brain_abs
        if known_decision_map is not None:
            resolved_decision_log = decision_log_path or ""
        else:
            resolved_decision_log = (
                decision_log_path
                if (fast_path and decision_log_path is not None and os.path.isabs(decision_log_path))
                else (
                    os.path.abspath(decision_log_path)
                    if decision_log_path is not None
                    else os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
                )
            )
        if canonical_atoms_root is not None and known_atom_bytes is not None:
            path = atom_path
            content_bytes = known_atom_bytes
            current_hash = known_atom_sha256 or content_sha256(content_bytes)
        else:
            path = _canonical_atom_path(atom_path, atoms_dir)
            content_bytes = Path(path).read_bytes()
            current_hash = content_sha256(content_bytes)
        pre_cached_verify: bool | None = None
        with _DECISION_CACHE_LOCK:
            cached_parse = _ATOM_ANCHOR_PARSE_CACHE.get(current_hash)
            if cached_parse is not None and has_pre_resolved_brain and cached_parse[0] and cached_parse[3]:
                pre_cached_verify = _ANCHOR_VERIFY_BY_HASH_CACHE.get(
                    (known_norm_brain_abs, current_hash, cached_parse[3])  # type: ignore[index]
                )
        if cached_parse is not None:
            ok_fm, anchors, clean_src_rel, recorded_src_sha, os_src_rel, preparsed_sources, preparsed_evidence = cached_parse
            if not ok_fm:
                return False
        else:
            content = content_bytes.decode("utf-8")
            parsed = split_frontmatter(content)
            if parsed is None or not is_memory_document(content):
                with _DECISION_CACHE_LOCK:
                    if len(_ATOM_ANCHOR_PARSE_CACHE) > 8192:
                        _ATOM_ANCHOR_PARSE_CACHE.clear()
                    _ATOM_ANCHOR_PARSE_CACHE[current_hash] = (False, {}, "", "", "", (), "")
                return False
            frontmatter, body_lines = parsed
            values = parse_frontmatter_values(frontmatter)
            sup_by = values.get("superseded_by", [])
            if isinstance(sup_by, str):
                sup_by = [sup_by] if sup_by.strip() else []
            anchors = {key: values.get(key) for key in ANCHOR_FIELDS}
            ok_fm = (
                not sup_by
                and values.get("review_status") == "accepted"
                and values.get("claim_state") == "human_reviewed"
                and all(anchors.values())
                and frontmatter_value(frontmatter, "citation_integrity") == "anchored"
            )
            if ok_fm:
                raw_sources = values.get("sources", [])
                if isinstance(raw_sources, str):
                    src_list = parse_scalar_list(raw_sources)
                else:
                    src_list = [str(v).strip() for v in raw_sources if str(v).strip()]
                preparsed_sources = tuple(src_list)
                preparsed_evidence = extract_evidence_from_body_lines(body_lines)
                raw_src_rel = str(anchors.get("evidence_source") or "").strip()
                src_rel_norm = raw_src_rel.replace("\\", "/")
                rec_sha = str(anchors.get("evidence_sha256") or "").strip().lower()
                if (
                    src_rel_norm
                    and len(rec_sha) == 64
                    and "\\" not in raw_src_rel
                    and ":" not in src_rel_norm
                    and "\x00" not in src_rel_norm
                    and not src_rel_norm.startswith("/")
                    and ".." not in src_rel_norm.split("/")
                    and src_rel_norm.startswith(("0-raw/", "sources/"))
                ):
                    clean_src_rel = src_rel_norm
                    recorded_src_sha = rec_sha
                    os_src_rel = src_rel_norm.replace("/", os.sep) if os.sep != "/" else src_rel_norm
                else:
                    clean_src_rel = ""
                    recorded_src_sha = ""
                    os_src_rel = ""
            else:
                preparsed_sources = ()
                preparsed_evidence = ""
                clean_src_rel = ""
                recorded_src_sha = ""
                os_src_rel = ""
            with _DECISION_CACHE_LOCK:
                if len(_ATOM_ANCHOR_PARSE_CACHE) > 8192:
                    _ATOM_ANCHOR_PARSE_CACHE.clear()
                _ATOM_ANCHOR_PARSE_CACHE[current_hash] = (
                    ok_fm,
                    anchors,
                    clean_src_rel,
                    recorded_src_sha,
                    os_src_rel,
                    preparsed_sources,
                    preparsed_evidence,
                )
            if not ok_fm:
                return False
        latest_map = known_decision_map if known_decision_map is not None else latest_decisions_by_atom(resolved_decision_log)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ReviewTransitionError):
        return False

    atom_fname = known_atom_filename or os.path.basename(path)
    latest = latest_map.get(atom_fname)
    if not latest or latest.get("action") != "approve" or latest.get("content_sha256_after") != current_hash:
        return False

    # Fast hash-verified source check: avoids re-running quote regexes when source file SHA-256 is unchanged.
    if clean_src_rel:
        if has_pre_resolved_brain:
            brain_abs = known_brain_abs  # type: ignore[assignment]
            norm_brain_abs = known_norm_brain_abs  # type: ignore[assignment]
        else:
            brain_abs = os.path.normpath(brain_dir) if (fast_path and ".." not in brain_dir) else os.path.abspath(brain_dir)
            norm_brain_abs = os.path.normcase(brain_abs)
        brain_prefix = brain_abs if brain_abs.endswith(os.sep) else (brain_abs + os.sep)
        src_candidate = brain_prefix + os_src_rel
        live_src_sha: str | None = None
        src_bytes: bytes | None = None
        if source_sha_memo is not None and src_candidate in source_sha_memo:
            memo_val = source_sha_memo[src_candidate]
            if memo_val is None:
                return False
            if isinstance(memo_val, tuple):
                live_src_sha, src_bytes = memo_val
            else:
                live_src_sha = str(memo_val)
            if live_src_sha != recorded_src_sha:
                return False
        else:
            src_parents_safe = True
            cur_dir = os.path.dirname(os.path.abspath(src_candidate))
            while True:
                try:
                    pst = os.lstat(cur_dir)
                except OSError:
                    src_parents_safe = False
                    break
                if (
                    not stat.S_ISDIR(pst.st_mode)
                    or stat.S_ISLNK(pst.st_mode)
                    or (os.name == "nt" and bool(getattr(pst, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))
                ):
                    src_parents_safe = False
                    break
                if os.path.normcase(cur_dir) == norm_brain_abs:
                    break
                next_dir = os.path.dirname(cur_dir)
                if next_dir == cur_dir:
                    src_parents_safe = False
                    break
                cur_dir = next_dir
            src_st = _lstat_regular_non_reparse(src_candidate) if src_parents_safe else None
            if src_st is not None:
                src_key = os.path.normcase(src_candidate if fast_path else os.path.abspath(src_candidate))
                use_src_stat_cache = len(latest_map) > LARGE_VAULT_STAT_CACHE_THRESHOLD
                if use_src_stat_cache:
                    with _DECISION_CACHE_LOCK:
                        cached_src = _SOURCE_STAT_SHA_CACHE.get(src_key)
                    if (
                        cached_src is not None
                        and cached_src[0] == src_st.st_mtime_ns
                        and cached_src[1] == src_st.st_ctime_ns
                        and cached_src[2] == src_st.st_size
                    ):
                        live_src_sha = cached_src[3]
                        if source_sha_memo is not None:
                            source_sha_memo[src_candidate] = live_src_sha
                        if live_src_sha != recorded_src_sha:
                            return False
                if live_src_sha is None:
                    try:
                        with open(src_candidate, "rb") as src_fh:
                            src_bytes = src_fh.read()
                        live_src_sha = content_sha256(src_bytes)
                        if use_src_stat_cache:
                            with _DECISION_CACHE_LOCK:
                                if len(_SOURCE_STAT_SHA_CACHE) > 4096:
                                    _SOURCE_STAT_SHA_CACHE.clear()
                                _SOURCE_STAT_SHA_CACHE[src_key] = (
                                    src_st.st_mtime_ns,
                                    src_st.st_ctime_ns,
                                    src_st.st_size,
                                    live_src_sha,
                                )
                        if source_sha_memo is not None:
                            source_sha_memo[src_candidate] = (live_src_sha, src_bytes)  # type: ignore[assignment]
                    except OSError:
                        if source_sha_memo is not None:
                            source_sha_memo[src_candidate] = None
                        return False
                    if live_src_sha != recorded_src_sha:
                        return False
            elif source_sha_memo is not None:
                source_sha_memo[src_candidate] = None

        if live_src_sha == recorded_src_sha:
            if pre_cached_verify is not None:
                return pre_cached_verify
            pair_key = (norm_brain_abs, current_hash, live_src_sha)
            with _DECISION_CACHE_LOCK:
                cached_verify = _ANCHOR_VERIFY_BY_HASH_CACHE.get(pair_key)
            if cached_verify is not None:
                return cached_verify
            if src_bytes is None and not has_cached_normalized_source(live_src_sha):
                try:
                    with open(src_candidate, "rb") as src_fh:
                        src_bytes = src_fh.read()
                    if content_sha256(src_bytes) != live_src_sha:
                        return False
                    if source_sha_memo is not None:
                        source_sha_memo[src_candidate] = (live_src_sha, src_bytes)  # type: ignore[assignment]
                except OSError:
                    return False
            if verify_evidence_anchor_known_source(
                content,
                anchors,
                source_rel=clean_src_rel,
                source_sha256=live_src_sha,
                source_bytes=src_bytes,
                preparsed_evidence=preparsed_evidence,
                preparsed_sources=preparsed_sources,
            ):
                verified = True
            else:
                if not content:
                    try:
                        content = content_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        return False
                verified = verify_evidence_anchor(content, anchors, brain_dir)
            with _DECISION_CACHE_LOCK:
                if len(_ANCHOR_VERIFY_BY_HASH_CACHE) > 8192:
                    _ANCHOR_VERIFY_BY_HASH_CACHE.clear()
                _ANCHOR_VERIFY_BY_HASH_CACHE[pair_key] = verified
            return verified

    if not content:
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return verify_evidence_anchor(content, anchors, brain_dir)
