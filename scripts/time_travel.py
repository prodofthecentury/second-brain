# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Time-Travel Vault Snapshots, Semantic Diff & Point-in-Time Okapi BM25 Recall.

Creates immutable, content-addressed checkpoints under brain/0-raw/archive/snapshots/
(with transparent read fallback to legacy brain/.sidecar/snapshots/) recording the
exact SHA-256 hashes, claims, evidence, and contexts of all owner-approved atoms
and playbooks at timestamp T using content-addressed SHA-256 blob deduplication
(schema_version 3). Supports:
  * Semantic diff between any two snapshots (or snapshot vs live vault), including
    atom claim/evidence diffs and playbook step/backing-atom diffs
  * Point-in-time field-weighted Okapi BM25 + corpus IDF + Porter-stem recall
    ("what did my verified brain know at snapshot T?") with zero leakage from
    later edits or unapproved drafts.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current, content_sha256, review_lock  # noqa: E402
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    resolve_module_vault_path,
    safe_file_in_dir,
    snapshot_module_defaults,
)
from cross_encoder import score_query_atom_pairs  # noqa: E402
from host_adapters import restrict_security_file_permissions  # noqa: E402
from lexical_store import porter_stem, tokenize  # noqa: E402
from memory_format import evaluate_temporal_validity, normalize_temporal_bound_str  # noqa: E402
from memory_review import parse_atom_record  # noqa: E402
from recall import QUERY_STOP_WORDS, normalize_project_tag, project_tags_match  # noqa: E402
from skill_playbooks import verify_playbook_integrity  # noqa: E402
from vault_ledger import _build_merkle_levels, _leaf_digest  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
_SNAP_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BM25_K1 = 1.5
_BM25_B = 0.75

_BLOB_CACHE_LOCK = threading.Lock()
_BLOB_MEM_CACHE: dict[str, dict[str, Any]] = {}
_MAX_BLOB_CACHE = 16_384

# Backward-compatible alias for any callers importing _light_stem
_light_stem = porter_stem


def snapshots_dir(brain_dir: str | None = None) -> Path:
    resolved_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    return Path(resolved_brain) / "0-raw" / "archive" / "snapshots"


def _snapshots_dir(brain_dir: str) -> Path:
    return snapshots_dir(brain_dir)


def _legacy_snapshots_dir(brain_dir: str) -> Path:
    return Path(brain_dir) / ".sidecar" / "snapshots"


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _blob_digest_and_bytes(record_payload: dict[str, Any]) -> tuple[str, bytes]:
    raw_bytes = _canonical_json(record_payload).encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest(), raw_bytes


def _write_blob_if_missing(blobs_root: Path, bhash: str, raw_bytes: bytes, record_payload: dict[str, Any]) -> None:
    if os.path.lexists(str(blobs_root)) and _is_reparse_or_link(str(blobs_root)):
        raise PermissionError("Refusing to write snapshot blob through symlinked blobs directory.")
    shard_dir = blobs_root / bhash[:2]
    if os.path.lexists(str(shard_dir)) and _is_reparse_or_link(str(shard_dir)):
        raise PermissionError("Refusing to write snapshot blob through symlinked shard directory.")
    shard_dir.mkdir(parents=True, exist_ok=True)
    blob_file = shard_dir / f"{bhash}.json"
    if os.path.lexists(str(blob_file)):
        if _is_reparse_or_link(str(blob_file)) or not blob_file.is_file():
            raise PermissionError(f"Refusing to use symlinked or non-regular snapshot blob: {blob_file}")
    else:
        fd, tmp_path = tempfile.mkstemp(dir=str(shard_dir), prefix=".blob-", suffix=".tmp")
        closed = False
        try:
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            if os.write(fd, raw_bytes) != len(raw_bytes):
                raise OSError(f"Incomplete snapshot blob write: {blob_file}")
            os.fsync(fd)
            os.close(fd)
            closed = True
            os.replace(tmp_path, blob_file)
            restrict_security_file_permissions(blob_file)
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
    with _BLOB_CACHE_LOCK:
        if len(_BLOB_MEM_CACHE) >= _MAX_BLOB_CACHE:
            _BLOB_MEM_CACHE.clear()
        _BLOB_MEM_CACHE[bhash] = json.loads(raw_bytes.decode("utf-8"))


def _read_and_verify_blob(
    bhash: str,
    *,
    blobs_roots: tuple[Path, ...],
    verify_hash: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    if not _HEX64_RE.fullmatch(bhash):
        return None, f"Invalid blob_sha256 {bhash!r}."
    blob_file: Path | None = None
    for root in blobs_roots:
        if os.path.lexists(str(root)) and _is_reparse_or_link(str(root)):
            return None, f"Snapshot blobs directory is a symlink or junction: {root}"
        shard = root / bhash[:2]
        if os.path.lexists(str(shard)) and _is_reparse_or_link(str(shard)):
            return None, f"Snapshot blob shard directory is a symlink or junction: {shard}"
        cand = shard / f"{bhash}.json"
        if os.path.lexists(str(cand)):
            if _is_reparse_or_link(str(cand)) or not cand.is_file():
                return None, f"Snapshot blob {bhash[:12]} is a symlink or non-regular file."
            blob_file = cand
            break
    if blob_file is None:
        return None, f"Missing snapshot blob {bhash[:12]}."

    if not verify_hash:
        with _BLOB_CACHE_LOCK:
            cached = _BLOB_MEM_CACHE.get(bhash)
        if cached is not None:
            return json.loads(_canonical_json(cached)), None

    try:
        raw_bytes = blob_file.read_bytes()
    except OSError as exc:
        return None, f"Unreadable snapshot blob {bhash[:12]}: {exc}"

    actual_hash = hashlib.sha256(raw_bytes).hexdigest()
    if actual_hash != bhash:
        with _BLOB_CACHE_LOCK:
            _BLOB_MEM_CACHE.pop(bhash, None)
        return None, f"Tampered snapshot blob (expected={bhash[:12]}, actual={actual_hash[:12]})."

    with _BLOB_CACHE_LOCK:
        cached = _BLOB_MEM_CACHE.get(bhash)
    if cached is not None:
        return dict(cached), None

    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"Corrupted JSON in snapshot blob {bhash[:12]}: {exc}"
    if not isinstance(parsed, dict):
        return None, f"Snapshot blob {bhash[:12]} is not a JSON object."

    with _BLOB_CACHE_LOCK:
        if len(_BLOB_MEM_CACHE) >= _MAX_BLOB_CACHE:
            _BLOB_MEM_CACHE.clear()
        _BLOB_MEM_CACHE[bhash] = parsed
    return dict(parsed), None


def _meaningful_tokens(text: str) -> list[str]:
    return [tok for tok in tokenize(str(text or "")) if len(tok) >= 2 and tok not in QUERY_STOP_WORDS]


def _matched_snippet(text: str, matched_terms: set[str], max_chars: int = 220) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) <= max_chars:
        return flat
    lower = flat.casefold()
    best_pos = 0
    for term in sorted(matched_terms, key=len, reverse=True):
        idx = lower.find(term.casefold())
        if idx >= 0:
            best_pos = max(0, idx - max_chars // 4)
            break
    window = flat[best_pos : best_pos + max_chars].strip()
    if best_pos > 0:
        window = "..." + window
    if best_pos + max_chars < len(flat):
        window = window + "..."
    return window


def _capture_live_state(
    *,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
) -> dict[str, Any]:
    brain_dir, atoms_dir = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    pb_dir = playbooks_dir if playbooks_dir is not None else os.path.join(brain_dir, "5-playbooks")
    sk_dir = skills_dir if skills_dir is not None else os.path.join(brain_dir, "6-skills")
    telemetry_db = os.path.join(brain_dir, ".sidecar", "telemetry.sqlite3")
    leaves: list[tuple[str, str]] = []

    atoms: dict[str, dict[str, Any]] = {}
    if os.path.isdir(atoms_dir) and not _is_reparse_or_link(atoms_dir):
        for entry in sorted(os.scandir(atoms_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            path = os.path.join(atoms_dir, entry.name)
            if _is_reparse_or_link(path) or not os.path.isfile(path):
                continue
            try:
                raw_bytes = Path(path).read_bytes()
                rec = parse_atom_record(path)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if (
                rec.get("review_status") != "accepted"
                or rec.get("superseded_by")
                or not approved_revision_is_current(path, brain_dir=brain_dir, atoms_dir=atoms_dir)
            ):
                continue
            fhash = content_sha256(raw_bytes)
            rel_path = f"2-atoms/{entry.name}"
            leaves.append((rel_path, _leaf_digest(rel_path, fhash)))
            atoms[entry.name] = {
                "atom_id": rec.get("id", ""),
                "filename": rec.get("filename", entry.name),
                "file_sha256": fhash,
                "memory_type": rec.get("memory_type", ""),
                "epistemic_label": rec.get("epistemic_label", "claim"),
                "epistemic_contract": rec.get("epistemic_contract", ""),
                "confidence": rec.get("confidence") or "",
                "importance": rec.get("importance") if rec.get("importance") is not None else "",
                "contexts": list(rec.get("contexts") or []),
                "claim": rec.get("claim") or "",
                "evidence": rec.get("evidence") or "",
                "valid_from": str(rec.get("valid_from") or ""),
                "valid_until": str(rec.get("valid_until") or ""),
            }

    playbooks: dict[str, dict[str, Any]] = {}
    if os.path.isdir(pb_dir) and not _is_reparse_or_link(pb_dir):
        for entry in sorted(os.scandir(pb_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            path = os.path.join(pb_dir, entry.name)
            if _is_reparse_or_link(path) or not os.path.isfile(path):
                continue
            pb_rep = verify_playbook_integrity(
                path,
                brain_dir=brain_dir,
                atoms_dir=atoms_dir,
                playbooks_dir=pb_dir,
                telemetry_db_path=telemetry_db,
            )
            if not pb_rep.valid or pb_rep.status != "active":
                continue
            try:
                raw_bytes = Path(path).read_bytes()
            except OSError:
                continue
            fhash = pb_rep.sha256 or content_sha256(raw_bytes)
            if content_sha256(raw_bytes) != fhash:
                continue
            rel_path = f"5-playbooks/{entry.name}"
            leaves.append((rel_path, _leaf_digest(rel_path, fhash)))
            playbooks[entry.name] = {
                "playbook_id": pb_rep.playbook_id or Path(entry.name).stem,
                "filename": pb_rep.filename or entry.name,
                "title": pb_rep.title or "",
                "trigger": pb_rep.trigger or "",
                "file_sha256": fhash,
                "contexts": list(pb_rep.contexts),
                "backing_atoms": list(pb_rep.backing_atoms),
                "steps": list(pb_rep.steps),
            }

    skills: dict[str, dict[str, Any]] = {}
    if os.path.isdir(sk_dir) and not _is_reparse_or_link(sk_dir):
        try:
            from executable_skills import verify_executable_skill_integrity
        except Exception:
            verify_executable_skill_integrity = None  # type: ignore[assignment]
        if verify_executable_skill_integrity is not None:
            for entry in sorted(os.scandir(sk_dir), key=lambda e: e.name):
                if not is_content_markdown(entry.name):
                    continue
                path = os.path.join(sk_dir, entry.name)
                if _is_reparse_or_link(path) or not os.path.isfile(path):
                    continue
                sk_rep = verify_executable_skill_integrity(
                    path,
                    brain_dir=brain_dir,
                    atoms_dir=atoms_dir,
                    playbooks_dir=pb_dir,
                    skills_dir=sk_dir,
                    telemetry_db_path=telemetry_db,
                )
                if not sk_rep.valid or sk_rep.status != "active":
                    continue
                try:
                    raw_bytes = Path(path).read_bytes()
                except OSError:
                    continue
                fhash = sk_rep.sha256 or content_sha256(raw_bytes)
                if content_sha256(raw_bytes) != fhash:
                    continue
                rel_path = f"6-skills/{entry.name}"
                leaves.append((rel_path, _leaf_digest(rel_path, fhash)))
                skills[entry.name] = {
                    "skill_id": sk_rep.skill_id or Path(entry.name).stem,
                    "filename": sk_rep.filename or entry.name,
                    "title": sk_rep.title or "",
                    "playbook_id": sk_rep.playbook_id or "",
                    "playbook_sha256": sk_rep.playbook_sha256 or "",
                    "file_sha256": fhash,
                    "contexts": list(sk_rep.contexts),
                    "backing_atoms": list(sk_rep.backing_atoms),
                    "step_actions_sha256": sk_rep.step_actions_sha256 or "",
                }

    leaves.sort(key=lambda item: item[0])
    merkle_root = _build_merkle_levels([leaf_hash for _, leaf_hash in leaves])[-1][0]
    return {
        "merkle_root": merkle_root,
        "atoms": atoms,
        "playbooks": playbooks,
        "skills": skills,
    }


def create_snapshot(
    *,
    label: str = "",
    actor: str = "owner",
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
) -> dict[str, Any]:
    """Create an immutable, content-addressed point-in-time snapshot under brain/0-raw/archive/snapshots/."""
    brain_dir, atoms_dir = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    clean_actor = (actor or "").strip()
    if not clean_actor:
        raise ValueError("Snapshot requires a non-empty actor.")

    snap_dir = _snapshots_dir(brain_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    if (
        _is_reparse_or_link(str(Path(brain_dir)))
        or _is_reparse_or_link(str(snap_dir.parent.parent))
        or _is_reparse_or_link(str(snap_dir.parent))
        or _is_reparse_or_link(str(snap_dir))
    ):
        raise PermissionError("Refusing to write snapshot through a symlink or junction.")

    lock_file = Path(brain_dir) / ".sidecar" / "writer.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with review_lock(str(lock_file)):
        if _is_reparse_or_link(str(snap_dir.parent)) or _is_reparse_or_link(str(snap_dir)):
            raise PermissionError("Refusing to write snapshot through a symlink or junction.")
        state = _capture_live_state(
            brain_dir=brain_dir,
            atoms_dir=atoms_dir,
            playbooks_dir=playbooks_dir,
            skills_dir=skills_dir,
        )
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        legacy_dir = _legacy_snapshots_dir(brain_dir)
        while True:
            entropy = secrets.token_hex(8)
            nonce = hashlib.sha256(f"{stamp}:{state['merkle_root']}:{label}:{entropy}".encode("utf-8")).hexdigest()[:8]
            snapshot_id = f"snap-{stamp}-{nonce}"
            target = snap_dir / f"{snapshot_id}.json"
            if not target.exists() and not (legacy_dir / f"{snapshot_id}.json").exists():
                break

        blobs_root = snap_dir / "blobs"
        atom_refs: dict[str, dict[str, str]] = {}
        for fname, atom_payload in sorted(state["atoms"].items()):
            bhash, raw_bytes = _blob_digest_and_bytes(atom_payload)
            _write_blob_if_missing(blobs_root, bhash, raw_bytes, atom_payload)
            atom_refs[fname] = {"file_sha256": str(atom_payload["file_sha256"]), "blob_sha256": bhash}

        playbook_refs: dict[str, dict[str, str]] = {}
        for pb_fname, pb_payload in sorted(state["playbooks"].items()):
            bhash, raw_bytes = _blob_digest_and_bytes(pb_payload)
            _write_blob_if_missing(blobs_root, bhash, raw_bytes, pb_payload)
            playbook_refs[pb_fname] = {"file_sha256": str(pb_payload["file_sha256"]), "blob_sha256": bhash}

        skill_refs: dict[str, dict[str, str]] = {}
        for sk_fname, sk_payload in sorted(state["skills"].items()):
            bhash, raw_bytes = _blob_digest_and_bytes(sk_payload)
            _write_blob_if_missing(blobs_root, bhash, raw_bytes, sk_payload)
            skill_refs[sk_fname] = {"file_sha256": str(sk_payload["file_sha256"]), "blob_sha256": bhash}

        manifest_doc: dict[str, Any] = {
            "schema_version": 3,
            "snapshot_id": snapshot_id,
            "created_at": now.isoformat(timespec="seconds"),
            "actor": clean_actor,
            "label": (label or "").strip()[:160],
            "merkle_root": state["merkle_root"],
            "atom_count": len(atom_refs),
            "playbook_count": len(playbook_refs),
            "skill_count": len(skill_refs),
            "atom_refs": atom_refs,
            "playbook_refs": playbook_refs,
            "skill_refs": skill_refs,
        }
        fd, tmp_path = tempfile.mkstemp(dir=str(snap_dir), prefix=".snap-", suffix=".tmp")
        try:
            os.write(fd, json.dumps(manifest_doc, indent=2, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, target)
        restrict_security_file_permissions(target)
        return {
            **manifest_doc,
            "atoms": state["atoms"],
            "playbooks": state["playbooks"],
            "skills": state["skills"],
        }


def _candidate_snapshot_dirs(brain_dir: str) -> list[Path]:
    primary = _snapshots_dir(brain_dir)
    legacy = _legacy_snapshots_dir(brain_dir)
    dirs = [primary]
    if os.path.normcase(os.path.abspath(str(legacy))) != os.path.normcase(os.path.abspath(str(primary))):
        dirs.append(legacy)
    return dirs


def _blobs_roots_for_brain(brain_dir: str) -> tuple[Path, ...]:
    return tuple(d / "blobs" for d in _candidate_snapshot_dirs(brain_dir))


def migrate_legacy_snapshots(*, brain_dir: str | None = None) -> dict[str, Any]:
    """Migrate verified legacy snapshots from brain/.sidecar/snapshots/ into durable brain/0-raw/archive/snapshots/."""
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    b_path = Path(brain_dir)
    if os.path.lexists(str(b_path)) and _is_reparse_or_link(str(b_path)):
        raise PermissionError("Refusing to migrate snapshots under symlinked vault directory.")
    primary = _snapshots_dir(brain_dir)
    legacy = _legacy_snapshots_dir(brain_dir)
    if os.path.normcase(os.path.abspath(str(primary))) == os.path.normcase(os.path.abspath(str(legacy))):
        return {"migrated_count": 0, "migrated_ids": [], "skipped_existing": [], "failed": []}
    if not legacy.exists():
        return {"migrated_count": 0, "migrated_ids": [], "skipped_existing": [], "failed": []}
    if _is_reparse_or_link(str(legacy.parent)) or _is_reparse_or_link(str(legacy)):
        raise PermissionError("Refusing to migrate snapshots from symlinked .sidecar/snapshots directory.")

    primary.mkdir(parents=True, exist_ok=True)
    if (
        _is_reparse_or_link(str(primary.parent.parent))
        or _is_reparse_or_link(str(primary.parent))
        or _is_reparse_or_link(str(primary))
    ):
        raise PermissionError("Refusing to migrate snapshots into symlinked 0-raw/archive/snapshots directory.")

    lock_file = b_path / ".sidecar" / "writer.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    migrated_ids: list[str] = []
    skipped_existing: list[str] = []
    failed: list[dict[str, Any]] = []

    with review_lock(str(lock_file)):
        legacy_blobs = legacy / "blobs"
        primary_blobs = primary / "blobs"
        if legacy_blobs.is_dir() and not _is_reparse_or_link(str(legacy_blobs)):
            for shard in sorted(legacy_blobs.iterdir(), key=lambda p: p.name):
                if not shard.is_dir() or _is_reparse_or_link(str(shard)):
                    continue
                for blob_file in sorted(shard.glob("*.json"), key=lambda p: p.name):
                    if _is_reparse_or_link(str(blob_file)) or not blob_file.is_file():
                        continue
                    bhash = blob_file.stem
                    if not _HEX64_RE.fullmatch(bhash):
                        continue
                    raw_b = blob_file.read_bytes()
                    if hashlib.sha256(raw_b).hexdigest() != bhash:
                        continue
                    parsed_b = json.loads(raw_b.decode("utf-8"))
                    if isinstance(parsed_b, dict):
                        _write_blob_if_missing(primary_blobs, bhash, raw_b, parsed_b)

        for entry in sorted(os.scandir(legacy), key=lambda e: e.name):
            if not entry.name.endswith(".json") or _is_reparse_or_link(entry.path) or not entry.is_file(follow_symlinks=False):
                continue
            fallback_id = entry.name[:-5]
            try:
                raw_bytes = Path(entry.path).read_bytes()
                data = json.loads(raw_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                failed.append({"snapshot_id": fallback_id, "errors": [f"Unreadable JSON: {exc}"]})
                continue
            rep = _verify_snapshot_doc(data, fallback_id=fallback_id, brain_dir=brain_dir)
            if not rep["valid"]:
                failed.append({"snapshot_id": rep["snapshot_id"], "errors": rep["errors"]})
                continue

            snap_id = rep["snapshot_id"]
            if not _SNAP_ID_RE.match(snap_id):
                failed.append({"snapshot_id": snap_id, "errors": ["Unsafe snapshot_id."]})
                continue
            dest = primary / f"{snap_id}.json"
            if dest.exists():
                if _is_reparse_or_link(str(dest)) or not dest.is_file():
                    failed.append({"snapshot_id": snap_id, "errors": ["Destination snapshot is irregular or symlinked."]})
                    continue
                dest_rep = verify_snapshot(snap_id, brain_dir=brain_dir)
                if dest_rep["valid"] and dest_rep["computed_merkle_root"] == rep["computed_merkle_root"]:
                    os.remove(entry.path)
                    skipped_existing.append(snap_id)
                else:
                    failed.append({"snapshot_id": snap_id, "errors": ["Conflicting snapshot already exists in 0-raw/archive/snapshots/."]})
                continue

            fd, tmp_path = tempfile.mkstemp(dir=str(primary), prefix=".mig-snap-", suffix=".tmp")
            closed = False
            try:
                if os.write(fd, raw_bytes) != len(raw_bytes):
                    raise OSError(f"Incomplete snapshot migration write for {snap_id}")
                os.fsync(fd)
                os.close(fd)
                closed = True
                os.replace(tmp_path, dest)
                restrict_security_file_permissions(dest)
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

            post_rep = verify_snapshot(snap_id, brain_dir=brain_dir)
            if not post_rep["valid"]:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                failed.append({"snapshot_id": snap_id, "errors": post_rep["errors"]})
                continue

            os.remove(entry.path)
            migrated_ids.append(snap_id)

        try:
            if not failed and legacy.is_dir() and not any(legacy.iterdir()):
                legacy.rmdir()
        except OSError:
            pass

    return {
        "migrated_count": len(migrated_ids),
        "migrated_ids": migrated_ids,
        "skipped_existing": skipped_existing,
        "failed": failed,
    }


def list_snapshots(*, brain_dir: str | None = None) -> list[dict[str, Any]]:
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for snap_dir in _candidate_snapshot_dirs(brain_dir):
        if not snap_dir.is_dir():
            continue
        if _is_reparse_or_link(str(snap_dir.parent)) or _is_reparse_or_link(str(snap_dir)):
            continue
        for entry in sorted(os.scandir(snap_dir), key=lambda e: e.name, reverse=True):
            if not entry.name.endswith(".json") or _is_reparse_or_link(entry.path) or not entry.is_file(follow_symlinks=False):
                continue
            try:
                data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                sid = str(data["snapshot_id"])
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                out.append(
                    {
                        "snapshot_id": sid,
                        "created_at": data["created_at"],
                        "label": data.get("label", ""),
                        "actor": data.get("actor", ""),
                        "merkle_root": data.get("merkle_root", ""),
                        "atom_count": data.get("atom_count", 0),
                        "playbook_count": data.get("playbook_count", 0),
                        "skill_count": data.get("skill_count", 0),
                        "schema_version": data.get("schema_version", 1),
                    }
                )
            except (OSError, json.JSONDecodeError, KeyError):
                continue
    out.sort(key=lambda item: (item["created_at"], item["snapshot_id"]), reverse=True)
    return out


def _hydrate_and_verify_snapshot_doc(
    data: Any,
    fallback_id: str = "",
    *,
    brain_dir: str | None = None,
    verify_blobs: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    errors: list[str] = []
    if not isinstance(data, dict):
        rep = {
            "valid": False,
            "snapshot_id": fallback_id,
            "recorded_merkle_root": "",
            "computed_merkle_root": "",
            "atom_count": 0,
            "playbook_count": 0,
            "skill_count": 0,
            "errors": ["Snapshot document is not a JSON object."],
        }
        return {}, rep

    snap_id = str(data.get("snapshot_id") or fallback_id)
    recorded_root = str(data.get("merkle_root") or "")
    schema_ver = data.get("schema_version", 1)
    blobs_roots = _blobs_roots_for_brain(brain_dir)

    hydrated_atoms: dict[str, dict[str, Any]] = {}
    hydrated_playbooks: dict[str, dict[str, Any]] = {}
    hydrated_skills: dict[str, dict[str, Any]] = {}

    if schema_ver == 3 or ("atom_refs" in data or "playbook_refs" in data or "skill_refs" in data):
        atom_refs = data.get("atom_refs")
        pb_refs = data.get("playbook_refs")
        has_skill_refs = "skill_refs" in data
        sk_refs = data.get("skill_refs") if has_skill_refs else {}
        if not isinstance(atom_refs, dict):
            errors.append("Snapshot 'atom_refs' map is missing or invalid.")
            atom_refs = {}
        if not isinstance(pb_refs, dict):
            errors.append("Snapshot 'playbook_refs' map is missing or invalid.")
            pb_refs = {}
        if not isinstance(sk_refs, dict):
            errors.append("Snapshot 'skill_refs' map is invalid.")
            sk_refs = {}
        for k, ref in sorted(atom_refs.items()):
            if not isinstance(ref, dict):
                errors.append(f"Invalid atom_ref entry for 2-atoms/{k}.")
                continue
            fhash = str(ref.get("file_sha256") or "")
            bhash = str(ref.get("blob_sha256") or "")
            if not _HEX64_RE.fullmatch(fhash):
                errors.append(f"Invalid atom file_sha256 for 2-atoms/{k}.")
            blob_obj, blob_err = _read_and_verify_blob(bhash, blobs_roots=blobs_roots, verify_hash=verify_blobs)
            if blob_err or blob_obj is None:
                errors.append(f"2-atoms/{k}: {blob_err or 'unreadable blob'}")
                hydrated_atoms[k] = {"filename": k, "file_sha256": fhash}
            else:
                if str(blob_obj.get("file_sha256") or "") != fhash:
                    errors.append(f"Atom blob file_sha256 mismatch for 2-atoms/{k}.")
                hydrated_atoms[k] = blob_obj
        for k, ref in sorted(pb_refs.items()):
            if not isinstance(ref, dict):
                errors.append(f"Invalid playbook_ref entry for 5-playbooks/{k}.")
                continue
            fhash = str(ref.get("file_sha256") or "")
            bhash = str(ref.get("blob_sha256") or "")
            if not _HEX64_RE.fullmatch(fhash):
                errors.append(f"Invalid playbook file_sha256 for 5-playbooks/{k}.")
            blob_obj, blob_err = _read_and_verify_blob(bhash, blobs_roots=blobs_roots, verify_hash=verify_blobs)
            if blob_err or blob_obj is None:
                errors.append(f"5-playbooks/{k}: {blob_err or 'unreadable blob'}")
                hydrated_playbooks[k] = {"filename": k, "file_sha256": fhash}
            else:
                if str(blob_obj.get("file_sha256") or "") != fhash:
                    errors.append(f"Playbook blob file_sha256 mismatch for 5-playbooks/{k}.")
                hydrated_playbooks[k] = blob_obj
        for k, ref in sorted(sk_refs.items()):
            if not isinstance(ref, dict):
                errors.append(f"Invalid skill_ref entry for 6-skills/{k}.")
                continue
            fhash = str(ref.get("file_sha256") or "")
            bhash = str(ref.get("blob_sha256") or "")
            if not _HEX64_RE.fullmatch(fhash):
                errors.append(f"Invalid skill file_sha256 for 6-skills/{k}.")
            blob_obj, blob_err = _read_and_verify_blob(bhash, blobs_roots=blobs_roots, verify_hash=verify_blobs)
            if blob_err or blob_obj is None:
                errors.append(f"6-skills/{k}: {blob_err or 'unreadable blob'}")
                hydrated_skills[k] = {"filename": k, "file_sha256": fhash}
            else:
                if str(blob_obj.get("file_sha256") or "") != fhash:
                    errors.append(f"Skill blob file_sha256 mismatch for 6-skills/{k}.")
                hydrated_skills[k] = blob_obj
        atom_source_map = atom_refs
        pb_source_map = pb_refs
        sk_source_map = sk_refs
    else:
        atoms = data.get("atoms")
        playbooks = data.get("playbooks")
        skills = data.get("skills") if "skills" in data else {}
        if not isinstance(atoms, dict):
            errors.append("Snapshot 'atoms' map is missing or invalid.")
            atoms = {}
        if not isinstance(playbooks, dict):
            errors.append("Snapshot 'playbooks' map is missing or invalid.")
            playbooks = {}
        if not isinstance(skills, dict):
            errors.append("Snapshot 'skills' map is invalid.")
            skills = {}
        hydrated_atoms = {k: (dict(v) if isinstance(v, dict) else {}) for k, v in atoms.items()}
        hydrated_playbooks = {k: (dict(v) if isinstance(v, dict) else {}) for k, v in playbooks.items()}
        hydrated_skills = {k: (dict(v) if isinstance(v, dict) else {}) for k, v in skills.items()}
        atom_source_map = atoms
        pb_source_map = playbooks
        sk_source_map = skills

    leaves: list[tuple[str, str]] = []
    for k, v in atom_source_map.items():
        fhash = str(v.get("file_sha256") or "") if isinstance(v, dict) else ""
        if not _HEX64_RE.fullmatch(fhash) and schema_ver != 3:
            errors.append(f"Invalid atom file_sha256 for 2-atoms/{k}.")
        rel_path = f"2-atoms/{k}"
        leaves.append((rel_path, _leaf_digest(rel_path, fhash)))
    for k, v in pb_source_map.items():
        fhash = str(v.get("file_sha256") or "") if isinstance(v, dict) else ""
        if not _HEX64_RE.fullmatch(fhash) and schema_ver != 3:
            errors.append(f"Invalid playbook file_sha256 for 5-playbooks/{k}.")
        rel_path = f"5-playbooks/{k}"
        leaves.append((rel_path, _leaf_digest(rel_path, fhash)))
    for k, v in sk_source_map.items():
        fhash = str(v.get("file_sha256") or "") if isinstance(v, dict) else ""
        if not _HEX64_RE.fullmatch(fhash) and schema_ver != 3:
            errors.append(f"Invalid skill file_sha256 for 6-skills/{k}.")
        rel_path = f"6-skills/{k}"
        leaves.append((rel_path, _leaf_digest(rel_path, fhash)))

    leaves.sort(key=lambda item: item[0])
    computed_root = _build_merkle_levels([d for _, d in leaves])[-1][0]
    if recorded_root != computed_root:
        errors.append(
            f"Merkle root mismatch (recorded={recorded_root[:12]}, computed={computed_root[:12]})."
        )

    rec_atom_count = data.get("atom_count")
    if isinstance(rec_atom_count, bool) or not isinstance(rec_atom_count, int) or rec_atom_count != len(atom_source_map):
        errors.append(f"atom_count mismatch (recorded={rec_atom_count!r}, actual={len(atom_source_map)}).")
    rec_pb_count = data.get("playbook_count")
    if isinstance(rec_pb_count, bool) or not isinstance(rec_pb_count, int) or rec_pb_count != len(pb_source_map):
        errors.append(f"playbook_count mismatch (recorded={rec_pb_count!r}, actual={len(pb_source_map)}).")
    if "skill_count" in data or "skill_refs" in data or "skills" in data:
        rec_sk_count = data.get("skill_count", 0)
        if isinstance(rec_sk_count, bool) or not isinstance(rec_sk_count, int) or rec_sk_count != len(sk_source_map):
            errors.append(f"skill_count mismatch (recorded={rec_sk_count!r}, actual={len(sk_source_map)}).")

    hydrated_doc = {
        **data,
        "atoms": hydrated_atoms,
        "playbooks": hydrated_playbooks,
        "skills": hydrated_skills,
    }
    report = {
        "valid": len(errors) == 0,
        "snapshot_id": snap_id,
        "recorded_merkle_root": recorded_root,
        "computed_merkle_root": computed_root,
        "atom_count": len(atom_source_map),
        "playbook_count": len(pb_source_map),
        "skill_count": len(sk_source_map),
        "errors": errors,
    }
    return hydrated_doc, report


def _verify_snapshot_doc(data: Any, fallback_id: str = "", *, brain_dir: str | None = None) -> dict[str, Any]:
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    _, rep = _hydrate_and_verify_snapshot_doc(data, fallback_id=fallback_id, brain_dir=brain_dir, verify_blobs=True)
    return rep


def _resolve_snapshot_file(snapshot_id: str, *, brain_dir: str | None = None) -> tuple[str, str]:
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    clean = (snapshot_id or "").strip()
    if clean.endswith(".json"):
        clean = clean[:-5]
    if not clean or not _SNAP_ID_RE.match(clean):
        raise ValueError(f"Invalid snapshot_id: {snapshot_id!r}")
    for snap_dir in _candidate_snapshot_dirs(brain_dir):
        if os.path.lexists(str(snap_dir.parent)) and _is_reparse_or_link(str(snap_dir.parent)):
            raise PermissionError("Refusing to load snapshot through a symlink or junction.")
        if os.path.lexists(str(snap_dir)) and _is_reparse_or_link(str(snap_dir)):
            raise PermissionError("Refusing to load snapshot through a symlink or junction.")
        if not snap_dir.is_dir():
            continue
        safe = safe_file_in_dir(str(snap_dir), f"{clean}.json", allowed_exts={".json"}, must_exist=True)
        if safe:
            if _is_reparse_or_link(safe):
                raise PermissionError("Refusing to load snapshot through a symlink or junction.")
            return clean, safe
    raise FileNotFoundError(f"Snapshot not found: {clean}")


def verify_snapshot(snapshot_id: str, *, brain_dir: str | None = None) -> dict[str, Any]:
    """Recompute and verify cryptographic Merkle + content-addressed blob integrity of a saved snapshot."""
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    clean, safe = _resolve_snapshot_file(snapshot_id, brain_dir=brain_dir)
    try:
        data = json.loads(Path(safe).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "snapshot_id": clean,
            "recorded_merkle_root": "",
            "computed_merkle_root": "",
            "atom_count": 0,
            "playbook_count": 0,
            "skill_count": 0,
            "errors": [f"Unreadable or malformed snapshot JSON: {exc}"],
        }
    return _verify_snapshot_doc(data, fallback_id=clean, brain_dir=brain_dir)


def load_snapshot(
    snapshot_id: str,
    *,
    brain_dir: str | None = None,
    verify_integrity: bool = True,
) -> dict[str, Any]:
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    clean, safe = _resolve_snapshot_file(snapshot_id, brain_dir=brain_dir)
    data = json.loads(Path(safe).read_text(encoding="utf-8"))
    hydrated, rep = _hydrate_and_verify_snapshot_doc(
        data,
        fallback_id=clean,
        brain_dir=brain_dir,
        verify_blobs=verify_integrity,
    )
    if verify_integrity and not rep["valid"]:
        raise ValueError(
            f"Snapshot Merkle integrity check failed for {clean}: {'; '.join(rep['errors'])}"
        )
    return hydrated


def _write_export_file_atomic(target: Path, text: str) -> None:
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing to write snapshot export through symlink: {target}")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".export-", suffix=".tmp")
    fd_closed = False
    try:
        encoded = text.replace("\r\n", "\n").encode("utf-8")
        if os.write(fd, encoded) != len(encoded):
            raise OSError(f"Incomplete export write: {target}")
        os.fsync(fd)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_name, target)
        restrict_security_file_permissions(target)
    finally:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def export_snapshot(
    snapshot_id: str,
    out_dir: str,
    *,
    brain_dir: str | None = None,
) -> dict[str, Any]:
    """Verify snapshot cryptographic integrity and export Markdown manifest + atom claim cards."""
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    if not out_dir or not str(out_dir).strip():
        raise ValueError("A non-empty export target directory is required.")
    raw_out = Path(str(out_dir).strip()).expanduser()
    if os.path.lexists(str(raw_out)) and _is_reparse_or_link(str(raw_out)):
        raise PermissionError(f"Refusing to export snapshot into symlinked directory: {raw_out}")
    out_path = raw_out.resolve()
    if os.path.lexists(str(out_path.parent)) and _is_reparse_or_link(str(out_path.parent)):
        raise PermissionError(f"Refusing to export snapshot under symlinked parent directory: {out_path.parent}")
    if out_path.exists():
        if not out_path.is_dir() or _is_reparse_or_link(str(out_path)):
            raise PermissionError(f"Export target is not a safe directory: {out_path}")
        if any(out_path.iterdir()):
            raise ValueError(f"Refusing to export snapshot into non-empty directory: {out_path}")
    else:
        out_path.mkdir(parents=True, exist_ok=False, mode=0o700)

    snap = load_snapshot(snapshot_id, brain_dir=brain_dir, verify_integrity=True)
    atoms_map: dict[str, dict[str, Any]] = snap.get("atoms", {})
    playbooks_map: dict[str, dict[str, Any]] = snap.get("playbooks", {})
    skills_map: dict[str, dict[str, Any]] = snap.get("skills", {})

    atoms_out = out_path / "atoms"
    atoms_out.mkdir(parents=True, exist_ok=True, mode=0o700)
    exported_files: list[str] = []

    manifest_lines = [
        f"# Snapshot Export: `{snap['snapshot_id']}`",
        "",
        f"- **Created At**: `{snap.get('created_at', '')}`",
        f"- **Actor**: `{snap.get('actor', '')}`",
        f"- **Label**: {snap.get('label', '') or '(none)'}",
        f"- **Merkle Root**: `{snap.get('merkle_root', '')}`",
        f"- **Verified Atoms**: {len(atoms_map)}",
        f"- **Accepted Playbooks**: {len(playbooks_map)}",
        f"- **Active Executable Skills**: {len(skills_map)}",
        "",
        "## Verified Atoms",
        "",
    ]

    for fname in sorted(atoms_map.keys()):
        atom = atoms_map[fname]
        safe_card = safe_file_in_dir(str(atoms_out), fname, allowed_exts={".md"}, must_exist=False)
        if not safe_card:
            raise ValueError(f"Unsafe atom filename in snapshot during export: {fname!r}")
        ep_contract = str(atom.get("epistemic_contract") or "").strip()
        ctx_list = [str(c) for c in (atom.get("contexts") or []) if str(c).strip()]
        card_lines = [
            "---",
            f"id: {atom.get('atom_id', Path(fname).stem)}",
            f"snapshot_id: {snap['snapshot_id']}",
            f"file_sha256: {atom.get('file_sha256', '')}",
            f"memory_type: {atom.get('memory_type', '')}",
            f"epistemic_label: {atom.get('epistemic_label', 'claim')}",
            *([f"epistemic_contract: {ep_contract}"] if ep_contract else []),
            f"contexts: {json.dumps(ctx_list, ensure_ascii=False)}",
            "---",
            "",
            "## Claim",
            str(atom.get("claim") or ""),
            "",
            "## Evidence",
            str(atom.get("evidence") or ""),
            "",
        ]
        _write_export_file_atomic(Path(safe_card), "\n".join(card_lines))
        exported_files.append(f"atoms/{os.path.basename(safe_card)}")
        manifest_lines.append(
            f"- `{fname}` (`{str(atom.get('file_sha256', ''))[:12]}`): {str(atom.get('claim') or '').strip()}"
        )

    if playbooks_map:
        manifest_lines.extend(["", "## Accepted Playbooks", ""])
        for pb_fname in sorted(playbooks_map.keys()):
            pb = playbooks_map[pb_fname]
            manifest_lines.append(
                f"- `{pb_fname}` (`{str(pb.get('file_sha256', ''))[:12]}`): {pb.get('title') or pb_fname}"
            )

    if skills_map:
        manifest_lines.extend(["", "## Active Executable Skills", ""])
        for sk_fname in sorted(skills_map.keys()):
            sk = skills_map[sk_fname]
            manifest_lines.append(
                f"- `{sk_fname}` (`{str(sk.get('file_sha256', ''))[:12]}`): {sk.get('title') or sk_fname}"
            )

    manifest_path = out_path / "MANIFEST.md"
    _write_export_file_atomic(manifest_path, "\n".join(manifest_lines) + "\n")
    return {
        "snapshot_id": snap["snapshot_id"],
        "out_dir": str(out_path),
        "manifest_path": str(manifest_path),
        "merkle_root": snap["merkle_root"],
        "atom_count": len(atoms_map),
        "playbook_count": len(playbooks_map),
        "skill_count": len(skills_map),
        "exported_files": exported_files,
    }


def diff_snapshots(
    from_snapshot_id: str,
    to_snapshot_id_or_live: str = "live",
    *,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    playbooks_dir: str | None = None,
    skills_dir: str | None = None,
) -> dict[str, Any]:
    """Compute a semantic diff between two snapshots (or snapshot vs live vault)."""
    brain_dir, atoms_dir = resolve_module_atoms_and_brain(globals(), atoms_dir=atoms_dir, brain_dir=brain_dir)
    before = load_snapshot(from_snapshot_id, brain_dir=brain_dir)
    if to_snapshot_id_or_live.strip().lower() == "live":
        after_state = _capture_live_state(
            brain_dir=brain_dir,
            atoms_dir=atoms_dir,
            playbooks_dir=playbooks_dir,
            skills_dir=skills_dir,
        )
        after_id = "live"
    else:
        after_doc = load_snapshot(to_snapshot_id_or_live, brain_dir=brain_dir)
        after_state = {
            "merkle_root": after_doc["merkle_root"],
            "atoms": after_doc["atoms"],
            "playbooks": after_doc["playbooks"],
            "skills": after_doc.get("skills", {}),
        }
        after_id = after_doc["snapshot_id"]

    a_before: dict[str, dict[str, Any]] = before.get("atoms", {})
    a_after: dict[str, dict[str, Any]] = after_state.get("atoms", {})
    added_atoms = [a_after[k] for k in sorted(a_after.keys() - a_before.keys())]
    removed_atoms = [a_before[k] for k in sorted(a_before.keys() - a_after.keys())]
    modified_atoms = [
        {
            "filename": k,
            "old_sha256": a_before[k]["file_sha256"],
            "new_sha256": a_after[k]["file_sha256"],
            "old_claim": a_before[k].get("claim", ""),
            "new_claim": a_after[k].get("claim", ""),
            "old_evidence": a_before[k].get("evidence", ""),
            "new_evidence": a_after[k].get("evidence", ""),
            "old_contexts": list(a_before[k].get("contexts") or []),
            "new_contexts": list(a_after[k].get("contexts") or []),
            "before_epistemic_label": a_before[k].get("epistemic_label", "claim"),
            "after_epistemic_label": a_after[k].get("epistemic_label", "claim"),
            "before_epistemic_contract": str(a_before[k].get("epistemic_contract") or ""),
            "after_epistemic_contract": str(a_after[k].get("epistemic_contract") or ""),
            "old_valid_from": str(a_before[k].get("valid_from") or ""),
            "new_valid_from": str(a_after[k].get("valid_from") or ""),
            "old_valid_until": str(a_before[k].get("valid_until") or ""),
            "new_valid_until": str(a_after[k].get("valid_until") or ""),
        }
        for k in sorted(a_before.keys() & a_after.keys())
        if a_before[k]["file_sha256"] != a_after[k]["file_sha256"]
    ]

    p_before: dict[str, dict[str, Any]] = before.get("playbooks", {})
    p_after: dict[str, dict[str, Any]] = after_state.get("playbooks", {})
    added_playbooks = sorted(p_after.keys() - p_before.keys())
    removed_playbooks = sorted(p_before.keys() - p_after.keys())
    modified_playbooks = [
        k for k in sorted(p_before.keys() & p_after.keys()) if p_before[k]["file_sha256"] != p_after[k]["file_sha256"]
    ]
    pb_added_items = [
        {
            "change_type": "added",
            "filename": k,
            "playbook_id": p_after[k].get("playbook_id", Path(k).stem),
            "title": p_after[k].get("title", ""),
            "old_sha256": None,
            "new_sha256": p_after[k].get("file_sha256", ""),
            "steps": list(p_after[k].get("steps") or []),
            "backing_atoms": list(p_after[k].get("backing_atoms") or []),
        }
        for k in added_playbooks
    ]
    pb_removed_items = [
        {
            "change_type": "removed",
            "filename": k,
            "playbook_id": p_before[k].get("playbook_id", Path(k).stem),
            "title": p_before[k].get("title", ""),
            "old_sha256": p_before[k].get("file_sha256", ""),
            "new_sha256": None,
            "steps": list(p_before[k].get("steps") or []),
            "backing_atoms": list(p_before[k].get("backing_atoms") or []),
        }
        for k in removed_playbooks
    ]
    pb_modified_items = [
        {
            "change_type": "modified",
            "filename": k,
            "playbook_id": p_after[k].get("playbook_id") or p_before[k].get("playbook_id", Path(k).stem),
            "title": p_after[k].get("title") or p_before[k].get("title", ""),
            "old_sha256": p_before[k].get("file_sha256", ""),
            "new_sha256": p_after[k].get("file_sha256", ""),
            "old_steps": list(p_before[k].get("steps") or []),
            "new_steps": list(p_after[k].get("steps") or []),
            "old_backing_atoms": list(p_before[k].get("backing_atoms") or []),
            "new_backing_atoms": list(p_after[k].get("backing_atoms") or []),
        }
        for k in modified_playbooks
    ]
    playbook_changes: dict[str, list[dict[str, Any]]] = {
        "added": pb_added_items,
        "removed": pb_removed_items,
        "modified": pb_modified_items,
    }

    s_before: dict[str, dict[str, Any]] = before.get("skills", {})
    s_after: dict[str, dict[str, Any]] = after_state.get("skills", {})
    added_skills = sorted(s_after.keys() - s_before.keys())
    removed_skills = sorted(s_before.keys() - s_after.keys())
    modified_skills = [
        k for k in sorted(s_before.keys() & s_after.keys()) if s_before[k]["file_sha256"] != s_after[k]["file_sha256"]
    ]
    skill_changes: dict[str, list[dict[str, Any]]] = {
        "added": [
            {
                "change_type": "added",
                "filename": k,
                "skill_id": s_after[k].get("skill_id", Path(k).stem),
                "title": s_after[k].get("title", ""),
                "old_sha256": None,
                "new_sha256": s_after[k].get("file_sha256", ""),
            }
            for k in added_skills
        ],
        "removed": [
            {
                "change_type": "removed",
                "filename": k,
                "skill_id": s_before[k].get("skill_id", Path(k).stem),
                "title": s_before[k].get("title", ""),
                "old_sha256": s_before[k].get("file_sha256", ""),
                "new_sha256": None,
            }
            for k in removed_skills
        ],
        "modified": [
            {
                "change_type": "modified",
                "filename": k,
                "skill_id": s_after[k].get("skill_id") or s_before[k].get("skill_id", Path(k).stem),
                "title": s_after[k].get("title") or s_before[k].get("title", ""),
                "old_sha256": s_before[k].get("file_sha256", ""),
                "new_sha256": s_after[k].get("file_sha256", ""),
            }
            for k in modified_skills
        ],
    }

    return {
        "from_snapshot": before["snapshot_id"],
        "to_snapshot": after_id,
        "merkle_root_before": before["merkle_root"],
        "merkle_root_after": after_state["merkle_root"],
        "unchanged": before["merkle_root"] == after_state["merkle_root"],
        "added_atoms": added_atoms,
        "removed_atoms": removed_atoms,
        "modified_atoms": modified_atoms,
        "added_playbooks": added_playbooks,
        "removed_playbooks": removed_playbooks,
        "modified_playbooks": modified_playbooks,
        "playbook_changes": playbook_changes,
        "added_skills": added_skills,
        "removed_skills": removed_skills,
        "modified_skills": modified_skills,
        "skill_changes": skill_changes,
        "summary": {
            "added": len(added_atoms),
            "removed": len(removed_atoms),
            "modified": len(modified_atoms),
        },
    }


def recall_at_snapshot(
    query: str,
    snapshot_id: str,
    *,
    project_scope: str | None = None,
    include_preferences: bool = False,
    top_k: int = 5,
    include_playbooks: bool = True,
    as_of: str | None = None,
    include_expired: bool = False,
    enable_reranker: bool = True,
    brain_dir: str | None = None,
) -> dict[str, Any]:
    """Field-weighted Okapi BM25 + bi-temporal validity + Cross-Encoder point-in-time recall over snapshot_id."""
    brain_dir = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    explicit_as_of = str(as_of).strip() if (as_of is not None and str(as_of).strip()) else None
    if explicit_as_of is not None:
        if len(explicit_as_of) > 64:
            raise ValueError("Invalid 'as_of' timestamp: exceeds 64 characters.")
        normalize_temporal_bound_str(explicit_as_of)

    snap = load_snapshot(snapshot_id, brain_dir=brain_dir)
    eff_as_of = explicit_as_of or (str(snap.get("created_at") or "").strip() or None)
    q_seq = _meaningful_tokens(query)
    if not q_seq:
        return {
            "snapshot_id": snap["snapshot_id"],
            "created_at": snap["created_at"],
            "as_of": eff_as_of,
            "merkle_root": snap["merkle_root"],
            "rerank_backend": "none",
            "results": [],
        }

    q_stems: list[str] = []
    stem_to_surface: dict[str, str] = {}
    for tok in q_seq:
        st = _light_stem(tok)
        stem_to_surface.setdefault(st, tok)
        if st not in q_stems:
            q_stems.append(st)
    q_stem_set = set(q_stems)
    q_bigrams = {f"{q_seq[i]} {q_seq[i + 1]}" for i in range(len(q_seq) - 1)}
    q_phrase = " ".join(q_seq) if len(q_seq) >= 2 else ""

    min_overlap = 2 if len(q_stem_set) >= 2 else 1
    target = normalize_project_tag(project_scope) if (project_scope and project_scope.strip()) else ""

    docs: list[tuple[str, str, dict[str, Any], Counter[str], float, set[str], str]] = []
    df_counts: Counter[str] = Counter()

    def _matches_proj(contexts: list[Any]) -> bool:
        if not target:
            return True
        clean_ctx = tuple(str(c).strip() for c in contexts if str(c).strip())
        return bool(project_tags_match(clean_ctx, (target,)))

    for fname, atom in sorted(snap.get("atoms", {}).items()):
        if not include_preferences and (
            atom.get("epistemic_label") == "preference" or bool(atom.get("epistemic_contract"))
        ):
            continue
        contexts = list(atom.get("contexts") or [])
        if not _matches_proj(contexts):
            continue

        vf_str = str(atom.get("valid_from") or "").strip()
        vu_str = str(atom.get("valid_until") or "").strip()
        tv = evaluate_temporal_validity(vf_str, vu_str, as_of=eff_as_of)
        if tv["status"] == "invalid_window":
            continue
        if not tv["active"] and not include_expired:
            continue

        claim_str = str(atom.get("claim") or "")
        ev_str = str(atom.get("evidence") or "")
        claim_toks = _meaningful_tokens(claim_str)
        evidence_toks = _meaningful_tokens(ev_str)
        id_toks = _meaningful_tokens(f"{Path(fname).stem} {atom.get('atom_id', '')}")
        ctx_toks = _meaningful_tokens(" ".join(str(c) for c in contexts))

        weighted_tf: Counter[str] = Counter()
        for t in claim_toks:
            weighted_tf[_light_stem(t)] += 3.0
        for t in evidence_toks:
            weighted_tf[_light_stem(t)] += 1.5
        for t in id_toks:
            weighted_tf[_light_stem(t)] += 1.5
        for t in ctx_toks:
            weighted_tf[_light_stem(t)] += 1.0

        doc_vocab = set(weighted_tf.keys())
        for st in doc_vocab:
            df_counts[st] += 1
        doc_len = float(sum(weighted_tf.values())) or 1.0
        full_text = f"{claim_str}\n{ev_str}".strip()
        atom_with_tv = {
            **atom,
            "valid_from": vf_str,
            "valid_until": vu_str,
            "temporal_status": str(tv["status"]),
        }
        docs.append(("atom", fname, atom_with_tv, weighted_tf, doc_len, doc_vocab, full_text))

    if include_playbooks:
        snap_atoms_map = snap.get("atoms", {})
        for pb_fname, pb in sorted(snap.get("playbooks", {}).items()):
            contexts = list(pb.get("contexts") or [])
            if not _matches_proj(contexts):
                continue
            backing_names = [str(b).strip() for b in (pb.get("backing_atoms") or []) if str(b).strip()]
            pb_backing_ok = True
            for b_name in backing_names:
                b_atom = snap_atoms_map.get(b_name)
                if not isinstance(b_atom, dict):
                    pb_backing_ok = False
                    break
                b_ctx = list(b_atom.get("contexts") or [])
                if target and not _matches_proj(b_ctx):
                    pb_backing_ok = False
                    break
                b_tv = evaluate_temporal_validity(
                    str(b_atom.get("valid_from") or "").strip(),
                    str(b_atom.get("valid_until") or "").strip(),
                    as_of=eff_as_of,
                )
                if b_tv["status"] == "invalid_window" or (not b_tv["active"] and not include_expired):
                    pb_backing_ok = False
                    break
            if not pb_backing_ok:
                continue
            title_str = str(pb.get("title") or "").strip()
            trigger_str = str(pb.get("trigger") or "").strip()
            steps_list = [str(s).strip() for s in (pb.get("steps") or []) if str(s).strip()]
            steps_str = " ".join(steps_list)
            if not (title_str or trigger_str or steps_str):
                continue
            pb_claim = f"{title_str}: {trigger_str}".strip(": ") if (title_str or trigger_str) else steps_list[0]
            head_toks = _meaningful_tokens(f"{title_str} {trigger_str}")
            step_toks = _meaningful_tokens(steps_str)
            id_toks = _meaningful_tokens(f"{Path(pb_fname).stem} {pb.get('playbook_id', '')}")
            ctx_toks = _meaningful_tokens(" ".join(str(c) for c in contexts))

            weighted_tf = Counter()
            for t in head_toks:
                weighted_tf[_light_stem(t)] += 1.8
            for t in step_toks:
                weighted_tf[_light_stem(t)] += 1.0
            for t in id_toks:
                weighted_tf[_light_stem(t)] += 1.2
            for t in ctx_toks:
                weighted_tf[_light_stem(t)] += 0.8

            doc_vocab = set(weighted_tf.keys())
            if not doc_vocab:
                continue
            for st in doc_vocab:
                df_counts[st] += 1
            doc_len = float(sum(weighted_tf.values())) or 1.0
            pb_record = {
                "atom_id": str(pb.get("playbook_id") or Path(pb_fname).stem),
                "playbook_id": str(pb.get("playbook_id") or Path(pb_fname).stem),
                "claim": pb_claim,
                "evidence": "\n".join(steps_list),
                "contexts": contexts,
                "file_sha256": pb.get("file_sha256", ""),
            }
            full_text = f"{pb_claim}\n{steps_str}".strip()
            docs.append(("playbook", pb_fname, pb_record, weighted_tf, doc_len, doc_vocab, full_text))

    if not docs:
        return {
            "snapshot_id": snap["snapshot_id"],
            "created_at": snap["created_at"],
            "as_of": eff_as_of,
            "merkle_root": snap["merkle_root"],
            "rerank_backend": "none",
            "results": [],
        }

    n_docs = len(docs)
    avgdl = sum(d[4] for d in docs) / max(1, n_docs)
    idf: dict[str, float] = {
        st: math.log(1.0 + (n_docs - df_counts[st] + 0.5) / (df_counts[st] + 0.5))
        for st in q_stem_set
    }

    scored: list[tuple[float, dict[str, Any]]] = []
    for kind, fname, rec, weighted_tf, doc_len, doc_vocab, full_text in docs:
        matched_stems = q_stem_set & doc_vocab
        if len(matched_stems) < min_overlap:
            continue
        bm25 = 0.0
        norm_factor = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (doc_len / max(1.0, avgdl)))
        for st in matched_stems:
            tf_val = weighted_tf[st]
            bm25 += idf[st] * ((tf_val * (_BM25_K1 + 1.0)) / (tf_val + norm_factor))

        norm_doc_words = " ".join(_meaningful_tokens(full_text))
        phrase_boost = 0.6 if (q_phrase and q_phrase in norm_doc_words) else 0.0
        bigram_hits = sum(1 for bg in q_bigrams if bg in norm_doc_words) if q_bigrams else 0
        bigram_boost = 0.25 * (bigram_hits / len(q_bigrams)) if q_bigrams else 0.0
        coverage_bonus = 0.25 * (len(matched_stems) / max(1, len(q_stem_set)))
        atom_bonus = 0.5 if kind == "atom" else 0.0
        score = bm25 + coverage_bonus + phrase_boost + bigram_boost + atom_bonus

        matched_surface = {stem_to_surface.get(st, st) for st in matched_stems}
        hit_obj: dict[str, Any] = {
            "kind": kind,
            "filename": fname,
            "atom_id": rec.get("atom_id", ""),
            "claim": str(rec.get("claim") or ""),
            "evidence": str(rec.get("evidence") or ""),
            "snippet": _matched_snippet(full_text, matched_surface),
            "contexts": list(rec.get("contexts") or []),
            "file_sha256": rec.get("file_sha256", ""),
            "matched_terms": sorted(matched_surface),
            "score": round(score, 4),
        }
        if kind == "atom":
            hit_obj["valid_from"] = str(rec.get("valid_from") or "")
            hit_obj["valid_until"] = str(rec.get("valid_until") or "")
            hit_obj["temporal_status"] = str(rec.get("temporal_status") or "unbounded")
        scored.append((score, hit_obj))

    scored.sort(key=lambda item: (-item[0], 0 if item[1]["kind"] == "atom" else 1, item[1]["filename"]))
    bounded_k = max(1, min(int(top_k), 25))
    rerank_backend = "none"
    if enable_reranker and scored:
        pool_n = min(len(scored), max(bounded_k * 3, 15))
        pool = scored[:pool_n]
        max_bm25 = max((s for s, _ in pool), default=1.0) or 1.0
        ce_docs = [
            f"## Claim\n{item[1]['claim']}\n\n## Evidence\n{item[1]['evidence']}"
            for item in pool
        ]
        ce_scores, rerank_backend = score_query_atom_pairs(query, ce_docs, allow_onnx=True)
        reranked_pool: list[tuple[float, dict[str, Any]]] = []
        for (raw_s, hit_d), ce_s in zip(pool, ce_scores):
            norm_b = max(0.0, min(1.0, raw_s / max_bm25))
            atom_tie = 0.05 if hit_d["kind"] == "atom" else 0.0
            if rerank_backend == "onnx_cross_encoder":
                blended = 0.30 * norm_b + 0.65 * float(ce_s) + atom_tie
            else:
                blended = 0.77 * norm_b + 0.18 * float(ce_s) + atom_tie
            upd_hit = {**hit_d, "score": round(blended, 4), "rerank_score": round(float(ce_s), 4)}
            reranked_pool.append((blended, upd_hit))
        reranked_pool.sort(key=lambda item: (-item[0], 0 if item[1]["kind"] == "atom" else 1, item[1]["filename"]))
        final_items = [item[1] for item in reranked_pool[:bounded_k]]
    else:
        final_items = [item[1] for item in scored[:bounded_k]]

    return {
        "snapshot_id": snap["snapshot_id"],
        "created_at": snap["created_at"],
        "as_of": eff_as_of,
        "merkle_root": snap["merkle_root"],
        "rerank_backend": rerank_backend,
        "results": final_items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time-Travel Vault Snapshots & Semantic Diff")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create a point-in-time vault snapshot")
    p_create.add_argument("--actor", required=True)
    p_create.add_argument("--label", default="")
    p_create.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="List saved snapshots")
    p_list.add_argument("--json", action="store_true")

    p_diff = sub.add_parser("diff", help="Diff two snapshots (or snapshot vs live)")
    p_diff.add_argument("from_snapshot")
    p_diff.add_argument("to_snapshot", nargs="?", default="live")
    p_diff.add_argument("--json", action="store_true")

    p_recall = sub.add_parser("recall", help="Recall only verified atoms/playbooks as of snapshot_id")
    p_recall.add_argument("snapshot_id")
    p_recall.add_argument("query")
    p_recall.add_argument("--project", default=None)
    p_recall.add_argument("--limit", "--top-k", dest="top_k", type=int, default=5)
    p_recall.add_argument("--as-of", default=None, help="Optional ISO-8601 date/timestamp override (defaults to snapshot created_at).")
    p_recall.add_argument("--include-expired", action="store_true", help="Include atoms expired or not yet valid at snapshot/as_of time.")
    p_recall.add_argument("--include-preferences", action="store_true")
    p_recall.add_argument("--no-playbooks", action="store_true")
    p_recall.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify cryptographic Merkle integrity of a snapshot")
    p_verify.add_argument("snapshot_id")
    p_verify.add_argument("--json", action="store_true")

    p_export = sub.add_parser("export", help="Export verified snapshot Markdown manifest and claim cards")
    p_export.add_argument("snapshot_id")
    p_export.add_argument("out_dir")
    p_export.add_argument("--json", action="store_true")

    p_migrate = sub.add_parser("migrate", help="Migrate legacy .sidecar/snapshots/*.json into durable 0-raw/archive/snapshots/")
    p_migrate.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "create":
        res = create_snapshot(label=args.label, actor=args.actor)
        print(json.dumps(res, indent=2) if args.json else f"Created {res['snapshot_id']} atoms={res['atom_count']}")
        return 0
    if args.cmd == "list":
        res = list_snapshots()
        print(json.dumps(res, indent=2) if args.json else f"{len(res)} snapshot(s)")
        return 0
    if args.cmd == "diff":
        res = diff_snapshots(args.from_snapshot, args.to_snapshot)
        print(
            json.dumps(res, indent=2)
            if args.json
            else f"added={len(res['added_atoms'])} removed={len(res['removed_atoms'])} modified={len(res['modified_atoms'])}"
        )
        return 0
    if args.cmd == "recall":
        res = recall_at_snapshot(
            args.query,
            args.snapshot_id,
            project_scope=args.project,
            top_k=args.top_k,
            as_of=args.as_of,
            include_expired=bool(args.include_expired),
            include_preferences=args.include_preferences,
            include_playbooks=not args.no_playbooks,
        )
        print(json.dumps(res, indent=2) if args.json else f"{len(res['results'])} result(s) at {res['snapshot_id']}")
        return 0
    if args.cmd == "verify":
        res = verify_snapshot(args.snapshot_id)
        print(
            json.dumps(res, indent=2)
            if args.json
            else f"snapshot={res['snapshot_id']} valid={res['valid']} merkle={res['computed_merkle_root'][:12]}"
        )
        return 0 if res["valid"] else 1
    if args.cmd == "export":
        res = export_snapshot(args.snapshot_id, args.out_dir)
        print(
            json.dumps(res, indent=2)
            if args.json
            else f"Exported {res['snapshot_id']} ({res['atom_count']} atom(s)) -> {res['out_dir']}"
        )
        return 0
    if args.cmd == "migrate":
        res = migrate_legacy_snapshots()
        print(
            json.dumps(res, indent=2)
            if args.json
            else f"Migrated {res['migrated_count']} snapshot(s): {', '.join(res['migrated_ids']) or '(none)'}"
        )
        return 1 if res["failed"] else 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
