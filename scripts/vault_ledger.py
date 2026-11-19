# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Cryptographic Vault Merkle Proof & Tamper-Evident Audit Ledger.

Builds a deterministic SHA-256 binary Merkle tree over all owner-approved
knowledge (accepted atoms under 2-atoms/ and accepted playbooks under
5-playbooks/), chains checkpoints into an append-only hash-linked ledger
(brain/0-raw/archive/vault-ledger.jsonl, with automatic migration from legacy
brain/.sidecar/vault-ledger.jsonl), and exports portable Merkle inclusion
proofs that an external verifier can validate offline without trusting the
running server.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current, content_sha256, review_lock  # noqa: E402
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    safe_file_in_dir,
)
from host_adapters import restrict_security_file_permissions  # noqa: E402
from memory_review import parse_atom_record  # noqa: E402
from skill_playbooks import verify_playbook_integrity  # noqa: E402

GENESIS_HASH = "0" * 64
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_O_BINARY = getattr(os, "O_BINARY", 0)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ledger_path(brain_dir: str = BRAIN_DIR) -> Path:
    return Path(brain_dir) / "0-raw" / "archive" / "vault-ledger.jsonl"


def _ledger_path(brain_dir: str = BRAIN_DIR) -> Path:
    return ledger_path(brain_dir)


def _legacy_ledger_path(brain_dir: str = BRAIN_DIR) -> Path:
    return Path(brain_dir) / ".sidecar" / "vault-ledger.jsonl"


def _leaf_digest(rel_path: str, file_sha256: str) -> str:
    return _sha256_text(f"leaf:{rel_path}:{file_sha256}")


def _pair_digest(left: str, right: str) -> str:
    return _sha256_text(f"node:{left}:{right}")


def _odd_digest(node: str) -> str:
    return _sha256_text(f"odd:{node}")


def _collect_verified_leaves(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> list[dict[str, str]]:
    """Collect sorted canonical leaves for all accepted atoms & playbooks."""
    pb_dir = playbooks_dir if playbooks_dir is not None else os.path.join(brain_dir, "5-playbooks")
    telemetry_db = os.path.join(brain_dir, ".sidecar", "telemetry.sqlite3")
    leaves: list[dict[str, str]] = []

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
            rel = f"2-atoms/{entry.name}"
            leaves.append(
                {
                    "rel_path": rel,
                    "kind": "atom",
                    "file_sha256": fhash,
                    "leaf_sha256": _leaf_digest(rel, fhash),
                }
            )

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
            rel = f"5-playbooks/{entry.name}"
            leaves.append(
                {
                    "rel_path": rel,
                    "kind": "playbook",
                    "file_sha256": fhash,
                    "leaf_sha256": _leaf_digest(rel, fhash),
                }
            )

    leaves.sort(key=lambda item: item["rel_path"])
    return leaves


collect_canonical_vault_leaves = _collect_verified_leaves


def _build_merkle_levels(leaf_hashes: list[str]) -> list[list[str]]:
    if not leaf_hashes:
        return [[_sha256_text("empty-vault-merkle-root")]]
    levels: list[list[str]] = [list(leaf_hashes)]
    cur = list(leaf_hashes)
    while len(cur) > 1:
        nxt: list[str] = []
        for idx in range(0, len(cur), 2):
            if idx + 1 < len(cur):
                nxt.append(_pair_digest(cur[idx], cur[idx + 1]))
            else:
                nxt.append(_odd_digest(cur[idx]))
        levels.append(nxt)
        cur = nxt
    return levels


def compute_vault_merkle(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Compute the deterministic Merkle root over all accepted atoms and playbooks."""
    leaves = _collect_verified_leaves(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
    levels = _build_merkle_levels([leaf["leaf_sha256"] for leaf in leaves])
    return {
        "merkle_root": levels[-1][0],
        "leaf_count": len(leaves),
        "approved_atoms": sum(1 for l in leaves if l["kind"] == "atom"),
        "accepted_playbooks": sum(1 for l in leaves if l["kind"] == "playbook"),
        "leaves": leaves,
    }


def _read_ledger_entries(ledger_file: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if os.path.lexists(str(ledger_file.parent)) and _is_reparse_or_link(str(ledger_file.parent)):
        return [], ["Ledger parent directory is a symlink or junction (rejected fail-closed)."]
    if os.path.lexists(str(ledger_file)) and _is_reparse_or_link(str(ledger_file)):
        return [], ["Ledger file is a symlink or junction (rejected fail-closed)."]
    if not ledger_file.exists():
        return [], []

    try:
        raw_text = ledger_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"Ledger unreadable or corrupted: {exc}"]

    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    expected_prev = GENESIS_HASH
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"Line {idx}: invalid JSON ({exc})")
            break
        if not isinstance(obj, dict):
            errors.append(f"Line {idx}: entry must be a JSON object")
            break
        recorded_hash = str(obj.get("entry_sha256") or "")
        body = {k: v for k, v in obj.items() if k != "entry_sha256"}
        computed_hash = _sha256_text(_canonical_json(body))
        if recorded_hash != computed_hash:
            errors.append(f"Line {idx}: entry_sha256 mismatch (recorded={recorded_hash[:12]}, computed={computed_hash[:12]})")
            break
        seq_val = obj.get("seq")
        if isinstance(seq_val, bool) or not isinstance(seq_val, int) or seq_val != len(entries) + 1:
            errors.append(f"Line {idx}: non-sequential seq={seq_val!r}")
            break
        if str(obj.get("prev_entry_sha256") or "") != expected_prev:
            errors.append(f"Line {idx}: broken prev_entry_sha256 chain")
            break
        expected_prev = recorded_hash
        entries.append(obj)
    return entries, errors


def _write_ledger_atomic(target: Path, entries: list[dict[str, Any]]) -> None:
    payload_bytes = "".join(_canonical_json(item) + "\n" for item in entries).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".ledger-", suffix=".tmp")
    closed = False
    try:
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        if os.write(fd, payload_bytes) != len(payload_bytes):
            raise OSError("Incomplete atomic write to vault-ledger.jsonl.")
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp_path, target)
        restrict_security_file_permissions(target)
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


def commit_ledger_checkpoint(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
    actor: str = "owner",
    note: str = "",
) -> dict[str, Any]:
    """Append a tamper-evident Merkle checkpoint to brain/0-raw/archive/vault-ledger.jsonl."""
    clean_actor = (actor or "").strip()
    if not clean_actor:
        raise ValueError("Ledger checkpoint requires a non-empty actor.")

    durable_file = ledger_path(brain_dir)
    legacy_file = _legacy_ledger_path(brain_dir)
    sidecar_dir = Path(brain_dir) / ".sidecar"

    raw_root = durable_file.parent.parent
    for check_dir in (raw_root, durable_file.parent, sidecar_dir):
        if os.path.lexists(str(check_dir)) and _is_reparse_or_link(str(check_dir)):
            raise PermissionError("Refusing to write ledger through a symlink or junction.")
    durable_file.parent.mkdir(parents=True, exist_ok=True)
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    for check_path in (durable_file.parent, sidecar_dir, durable_file, legacy_file):
        if os.path.lexists(str(check_path)) and _is_reparse_or_link(str(check_path)):
            raise PermissionError("Refusing to write ledger through a symlink or junction.")

    with review_lock(str(sidecar_dir / "writer.lock")):
        for check_path in (durable_file.parent, durable_file, legacy_file):
            if os.path.lexists(str(check_path)) and _is_reparse_or_link(str(check_path)):
                raise PermissionError("Refusing to write ledger through a symlink or junction.")

        durable_exists = os.path.lexists(str(durable_file))
        legacy_exists = os.path.lexists(str(legacy_file))

        if durable_exists:
            entries, errors = _read_ledger_entries(durable_file)
            if legacy_exists:
                _, leg_errors = _read_ledger_entries(legacy_file)
                errors.extend(f"Legacy .sidecar ledger error: {e}" for e in leg_errors)
            if errors:
                raise ValueError(f"Cannot append to tampered/corrupted vault ledger: {'; '.join(errors)}")
        elif legacy_exists:
            entries, errors = _read_ledger_entries(legacy_file)
            if errors:
                raise ValueError(f"Cannot migrate tampered/corrupted legacy vault ledger: {'; '.join(errors)}")
        else:
            entries = []

        merkle = compute_vault_merkle(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
        prev_hash = entries[-1]["entry_sha256"] if entries else GENESIS_HASH
        payload: dict[str, Any] = {
            "schema_version": 1,
            "seq": len(entries) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": clean_actor,
            "note": (note or "").strip()[:300],
            "prev_entry_sha256": prev_hash,
            "merkle_root": merkle["merkle_root"],
            "leaf_count": merkle["leaf_count"],
            "approved_atoms": merkle["approved_atoms"],
            "accepted_playbooks": merkle["accepted_playbooks"],
        }
        payload["entry_sha256"] = _sha256_text(_canonical_json(payload))

        if not durable_exists and legacy_exists:
            _write_ledger_atomic(durable_file, [*entries, payload])
            try:
                legacy_file.unlink()
            except FileNotFoundError:
                pass
        else:
            encoded_line = (_canonical_json(payload) + "\n").encode("utf-8")
            fd = os.open(str(durable_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o600)
            try:
                if os.write(fd, encoded_line) != len(encoded_line):
                    raise OSError("Incomplete write to vault-ledger.jsonl.")
                os.fsync(fd)
            finally:
                os.close(fd)
            restrict_security_file_permissions(durable_file)
        return payload


def verify_vault_ledger(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Verify hash-chain integrity of vault-ledger.jsonl and compare against live Merkle root."""
    durable_file = ledger_path(brain_dir)
    legacy_file = _legacy_ledger_path(brain_dir)
    raw_root = durable_file.parent.parent

    if os.path.lexists(str(raw_root)) and _is_reparse_or_link(str(raw_root)):
        entries: list[dict[str, Any]] = []
        errors: list[str] = ["Ledger 0-raw directory is a symlink or junction (rejected fail-closed)."]
    elif os.path.lexists(str(durable_file)):
        entries, errors = _read_ledger_entries(durable_file)
        if os.path.lexists(str(legacy_file)):
            _, leg_errors = _read_ledger_entries(legacy_file)
            if leg_errors:
                errors.extend(f"Legacy .sidecar ledger error: {e}" for e in leg_errors)
    elif os.path.lexists(str(legacy_file)):
        entries, errors = _read_ledger_entries(legacy_file)
    else:
        _, dur_err = _read_ledger_entries(durable_file)
        _, leg_err = _read_ledger_entries(legacy_file)
        entries, errors = [], [*dur_err, *leg_err]

    merkle = compute_vault_merkle(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
    latest = entries[-1] if entries else None
    chain_valid = len(errors) == 0
    synced_with_head = bool(latest and latest.get("merkle_root") == merkle["merkle_root"])
    return {
        "chain_valid": chain_valid,
        "entry_count": len(entries),
        "entries": entries,
        "errors": errors,
        "head_seq": latest["seq"] if latest else 0,
        "head_entry_sha256": latest["entry_sha256"] if latest else None,
        "head_merkle_root": latest["merkle_root"] if latest else None,
        "live_merkle_root": merkle["merkle_root"],
        "live_leaf_count": merkle["leaf_count"],
        "leaf_count": merkle["leaf_count"],
        "approved_atoms": merkle["approved_atoms"],
        "accepted_playbooks": merkle["accepted_playbooks"],
        "synced_with_head": synced_with_head,
        "matches_latest_checkpoint": synced_with_head,
    }


def _build_proof_from_merkle(
    target_rel: str,
    *,
    merkle: dict[str, Any],
    brain_dir: str,
    atoms_dir: str,
    playbooks_dir: str | None,
    include_kind: bool = True,
    not_member_label: str = "",
) -> dict[str, Any]:
    leaves = merkle["leaves"]
    idx = next((i for i, leaf in enumerate(leaves) if leaf["rel_path"] == target_rel), None)
    if idx is None:
        label = not_member_label or target_rel
        raise PermissionError(f"Leaf '{label}' is not an accepted member of the verified vault Merkle tree.")

    levels = _build_merkle_levels([leaf["leaf_sha256"] for leaf in leaves])
    siblings: list[dict[str, str]] = []
    cur_idx = idx
    for level in levels[:-1]:
        if cur_idx % 2 == 0:
            if cur_idx + 1 < len(level):
                siblings.append({"position": "right", "sha256": level[cur_idx + 1]})
            else:
                siblings.append({"position": "odd", "sha256": level[cur_idx]})
        else:
            siblings.append({"position": "left", "sha256": level[cur_idx - 1]})
        cur_idx //= 2

    ledger = verify_vault_ledger(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
    proof: dict[str, Any] = {
        "schema_version": 1,
        "rel_path": target_rel,
        "file_sha256": leaves[idx]["file_sha256"],
        "leaf_sha256": leaves[idx]["leaf_sha256"],
        "siblings": siblings,
        "merkle_root": merkle["merkle_root"],
        "ledger_head_entry_sha256": ledger["head_entry_sha256"] if ledger["synced_with_head"] else None,
    }
    if include_kind:
        proof["kind"] = leaves[idx]["kind"]
    return proof


def prove_leaf_membership(
    target: str,
    *,
    kind: str = "auto",
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Generate a portable Merkle inclusion proof for a verified atom or active playbook leaf."""
    clean_kind = str(kind or "auto").strip().lower()
    if clean_kind not in {"auto", "atom", "playbook"}:
        raise ValueError(f"Unsupported leaf kind: {kind!r}. Expected 'auto', 'atom', or 'playbook'.")

    eff_atoms_dir = (
        os.path.join(brain_dir, "2-atoms")
        if atoms_dir == ATOMS_DIR and os.path.abspath(brain_dir) != os.path.abspath(BRAIN_DIR)
        else atoms_dir
    )
    if playbooks_dir is not None:
        eff_pb_dir = playbooks_dir
    elif os.path.abspath(brain_dir) != os.path.abspath(BRAIN_DIR):
        eff_pb_dir = os.path.join(brain_dir, "5-playbooks")
    else:
        eff_pb_dir = PLAYBOOKS_DIR

    raw_target = str(target or "").strip()
    if not raw_target:
        raise FileNotFoundError("Leaf target must be non-empty.")

    # Normalize explicit 'playbook:<name>' prefix while rejecting any other colon/drive/path tricks.
    if raw_target.startswith("playbook:"):
        raw_target = f"5-playbooks/{raw_target[len('playbook:'):]}"
    if (
        "\x00" in raw_target
        or ":" in raw_target
        or raw_target.startswith(("/", "\\"))
        or os.path.isabs(raw_target)
    ):
        raise PermissionError(f"Unsafe or absolute leaf target rejected: {target!r}")

    norm_rel = raw_target.replace("\\", "/")
    parts = norm_rel.split("/")
    if any(seg in {"", ".", ".."} for seg in parts):
        raise PermissionError(f"Path traversal or malformed leaf target rejected: {target!r}")

    def _resolve_leaf_file(base_dir: str, basename: str, prefix: str) -> str:
        if os.path.lexists(base_dir) and _is_reparse_or_link(base_dir):
            raise PermissionError(f"Refusing to resolve leaf under symlinked directory: {base_dir}")
        md_name = basename if basename.lower().endswith(".md") else f"{basename}.md"
        candidate_raw = os.path.join(base_dir, md_name)
        if os.path.lexists(candidate_raw) and _is_reparse_or_link(candidate_raw):
            raise PermissionError(f"Refusing to prove symlinked or reparse-point leaf: {target!r}")
        safe_path = safe_file_in_dir(base_dir, md_name, allowed_exts={".md"}, must_exist=True)
        if not safe_path or not os.path.isfile(safe_path) or _is_reparse_or_link(safe_path):
            raise FileNotFoundError(f"Leaf target not found: {target}")
        return f"{prefix}/{os.path.basename(safe_path)}"

    if len(parts) == 2 and parts[0] == "2-atoms":
        if clean_kind == "playbook":
            raise PermissionError(f"Leaf prefix '2-atoms/' conflicts with kind='playbook': {target!r}")
        target_rel = _resolve_leaf_file(eff_atoms_dir, parts[1], "2-atoms")
    elif len(parts) == 2 and parts[0] in {"5-playbooks", "playbooks"}:
        if clean_kind == "atom":
            raise PermissionError(f"Leaf prefix '{parts[0]}/' conflicts with kind='atom': {target!r}")
        target_rel = _resolve_leaf_file(eff_pb_dir, parts[1], "5-playbooks")
    elif len(parts) > 1:
        raise PermissionError(f"Unsupported leaf directory prefix in {target!r}")
    else:
        basename = parts[0]
        if clean_kind == "atom":
            target_rel = _resolve_leaf_file(eff_atoms_dir, basename, "2-atoms")
        elif clean_kind == "playbook":
            target_rel = _resolve_leaf_file(eff_pb_dir, basename, "5-playbooks")
        else:
            # Auto-detect across 2-atoms/ and 5-playbooks/, rejecting symlinks fail-closed first.
            md_name = basename if basename.lower().endswith(".md") else f"{basename}.md"
            for check_dir in (eff_atoms_dir, eff_pb_dir):
                if os.path.lexists(check_dir) and _is_reparse_or_link(check_dir):
                    raise PermissionError(f"Refusing to resolve leaf under symlinked directory: {check_dir}")
                cand = os.path.join(check_dir, md_name)
                if os.path.lexists(cand) and _is_reparse_or_link(cand):
                    raise PermissionError(f"Refusing to prove symlinked or reparse-point leaf: {target!r}")
            atom_safe = (
                safe_file_in_dir(eff_atoms_dir, md_name, allowed_exts={".md"}, must_exist=True)
                if os.path.isdir(eff_atoms_dir)
                else None
            )
            pb_safe = (
                safe_file_in_dir(eff_pb_dir, md_name, allowed_exts={".md"}, must_exist=True)
                if os.path.isdir(eff_pb_dir)
                else None
            )
            merkle = compute_vault_merkle(brain_dir=brain_dir, atoms_dir=eff_atoms_dir, playbooks_dir=eff_pb_dir)
            leaf_paths = {leaf["rel_path"] for leaf in merkle["leaves"]}
            atom_rel = f"2-atoms/{os.path.basename(atom_safe)}" if atom_safe else None
            pb_rel = f"5-playbooks/{os.path.basename(pb_safe)}" if pb_safe else None
            in_atom = bool(atom_rel and atom_rel in leaf_paths)
            in_pb = bool(pb_rel and pb_rel in leaf_paths)
            if in_atom and in_pb:
                raise PermissionError(
                    f"Ambiguous leaf target '{target}': present in both 2-atoms/ and 5-playbooks/; specify kind."
                )
            if in_atom and atom_rel is not None:
                return _build_proof_from_merkle(
                    atom_rel,
                    merkle=merkle,
                    brain_dir=brain_dir,
                    atoms_dir=eff_atoms_dir,
                    playbooks_dir=eff_pb_dir,
                    include_kind=True,
                    not_member_label=target,
                )
            if in_pb and pb_rel is not None:
                return _build_proof_from_merkle(
                    pb_rel,
                    merkle=merkle,
                    brain_dir=brain_dir,
                    atoms_dir=eff_atoms_dir,
                    playbooks_dir=eff_pb_dir,
                    include_kind=True,
                    not_member_label=target,
                )
            if atom_safe or pb_safe:
                raise PermissionError(
                    f"Target '{target}' is not an accepted member of the verified vault Merkle tree."
                )
            raise FileNotFoundError(f"Leaf target not found: {target}")

    merkle = compute_vault_merkle(brain_dir=brain_dir, atoms_dir=eff_atoms_dir, playbooks_dir=eff_pb_dir)
    return _build_proof_from_merkle(
        target_rel,
        merkle=merkle,
        brain_dir=brain_dir,
        atoms_dir=eff_atoms_dir,
        playbooks_dir=eff_pb_dir,
        include_kind=True,
        not_member_label=target,
    )


def prove_atom_membership(
    atom_filename: str,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Generate a portable Merkle inclusion proof for an accepted atom or playbook leaf."""
    return prove_leaf_membership(
        atom_filename,
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        playbooks_dir=playbooks_dir,
    )


def verify_merkle_proof(proof: dict[str, Any]) -> bool:
    """Offline stateless verification of a Merkle inclusion proof bundle."""
    if not isinstance(proof, dict):
        return False
    rel_path = str(proof.get("rel_path") or "")
    file_sha256 = str(proof.get("file_sha256") or "").lower()
    leaf_sha256 = str(proof.get("leaf_sha256") or "").lower()
    expected_root = str(proof.get("merkle_root") or "").lower()
    siblings = proof.get("siblings")
    if siblings is None:
        siblings = []
    if not isinstance(siblings, list):
        return False
    if (
        not rel_path
        or not _HEX64_RE.fullmatch(file_sha256)
        or not _HEX64_RE.fullmatch(leaf_sha256)
        or not _HEX64_RE.fullmatch(expected_root)
    ):
        return False
    if _leaf_digest(rel_path, file_sha256) != leaf_sha256:
        return False

    cur = leaf_sha256
    for step in siblings:
        if not isinstance(step, dict):
            return False
        pos = step.get("position")
        sib = str(step.get("sha256") or "").lower()
        if not _HEX64_RE.fullmatch(sib):
            return False
        if pos == "right":
            cur = _pair_digest(cur, sib)
        elif pos == "left":
            cur = _pair_digest(sib, cur)
        elif pos == "odd":
            if sib != cur:
                return False
            cur = _odd_digest(cur)
        else:
            return False
    return cur == expected_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cryptographic Vault Merkle Proof & Audit Ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_commit = sub.add_parser("commit", help="Append a tamper-evident Merkle checkpoint")
    p_commit.add_argument("--actor", required=True)
    p_commit.add_argument("--note", default="")
    p_commit.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify vault ledger chain and live Merkle root")
    p_verify.add_argument("--json", action="store_true")

    p_prove = sub.add_parser("prove", help="Generate offline Merkle proof for a verified atom or playbook leaf")
    p_prove.add_argument("atom", help="Atom or playbook reference (e.g. 2-atoms/<name>, 5-playbooks/<name>, or <name>)")
    p_prove.add_argument("--kind", choices=["auto", "atom", "playbook"], default="auto")
    p_prove.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "commit":
        res = commit_ledger_checkpoint(actor=args.actor, note=args.note)
        print(json.dumps(res, indent=2) if args.json else f"Committed seq={res['seq']} root={res['merkle_root'][:16]}...")
        return 0
    if args.cmd == "verify":
        res = verify_vault_ledger()
        print(json.dumps(res, indent=2) if args.json else f"chain_valid={res['chain_valid']} synced={res['synced_with_head']} root={res['live_merkle_root'][:16]}...")
        return 0 if res["chain_valid"] else 1
    if args.cmd == "prove":
        res = prove_leaf_membership(args.atom, kind=args.kind)
        print(json.dumps(res, indent=2) if args.json else f"Proof for {res['rel_path']} root={res['merkle_root'][:16]}... valid={verify_merkle_proof(res)}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
