# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Cryptographic Vault Merkle Proof & Tamper-Evident Audit Ledger.

Builds a deterministic SHA-256 binary Merkle tree over all owner-approved
knowledge (accepted atoms under 2-atoms/ and accepted playbooks under
5-playbooks/), chains checkpoints into an append-only hash-linked ledger
(brain/.sidecar/vault-ledger.jsonl), and exports portable Merkle inclusion
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
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current, content_sha256, review_lock  # noqa: E402
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    PLAYBOOKS_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    safe_file_in_dir,
)
from memory_review import parse_atom_record  # noqa: E402
from skill_playbooks import verify_playbook_integrity  # noqa: E402

GENESIS_HASH = "0" * 64


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ledger_path(brain_dir: str) -> Path:
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
    if not ledger_file.exists():
        return [], []
    if _is_reparse_or_link(str(ledger_file)):
        return [], ["Ledger file is a symlink or junction (rejected fail-closed)."]

    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    expected_prev = GENESIS_HASH
    for idx, line in enumerate(ledger_file.read_text(encoding="utf-8").splitlines(), start=1):
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
        if int(obj.get("seq", -1)) != len(entries) + 1:
            errors.append(f"Line {idx}: non-sequential seq={obj.get('seq')}")
            break
        if str(obj.get("prev_entry_sha256") or "") != expected_prev:
            errors.append(f"Line {idx}: broken prev_entry_sha256 chain")
            break
        expected_prev = recorded_hash
        entries.append(obj)
    return entries, errors


def commit_ledger_checkpoint(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
    actor: str = "owner",
    note: str = "",
) -> dict[str, Any]:
    """Append a tamper-evident Merkle checkpoint to brain/.sidecar/vault-ledger.jsonl."""
    clean_actor = (actor or "").strip()
    if not clean_actor:
        raise ValueError("Ledger checkpoint requires a non-empty actor.")

    ledger_file = _ledger_path(brain_dir)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(ledger_file.parent)) or (
        os.path.lexists(str(ledger_file)) and _is_reparse_or_link(str(ledger_file))
    ):
        raise PermissionError("Refusing to write ledger through a symlink or junction.")

    with review_lock(str(ledger_file.parent / "review.lock")):
        if os.path.lexists(str(ledger_file)) and _is_reparse_or_link(str(ledger_file)):
            raise PermissionError("Refusing to write ledger through a symlink or junction.")
        entries, errors = _read_ledger_entries(ledger_file)
        if errors:
            raise ValueError(f"Cannot append to tampered/corrupted vault ledger: {'; '.join(errors)}")

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

        with open(ledger_file, "a", encoding="utf-8") as fh:
            fh.write(_canonical_json(payload) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return payload


def verify_vault_ledger(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Verify hash-chain integrity of vault-ledger.jsonl and compare against live Merkle root."""
    ledger_file = _ledger_path(brain_dir)
    entries, errors = _read_ledger_entries(ledger_file)
    merkle = compute_vault_merkle(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
    latest = entries[-1] if entries else None
    chain_valid = len(errors) == 0
    synced_with_head = bool(latest and latest.get("merkle_root") == merkle["merkle_root"])
    return {
        "chain_valid": chain_valid,
        "entry_count": len(entries),
        "errors": errors,
        "head_seq": latest["seq"] if latest else 0,
        "head_entry_sha256": latest["entry_sha256"] if latest else None,
        "head_merkle_root": latest["merkle_root"] if latest else None,
        "live_merkle_root": merkle["merkle_root"],
        "live_leaf_count": merkle["leaf_count"],
        "synced_with_head": synced_with_head,
    }


def prove_atom_membership(
    atom_filename: str,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Generate a portable Merkle inclusion proof for an accepted atom."""
    safe_path = safe_file_in_dir(atoms_dir, atom_filename, allowed_exts={".md"}, must_exist=True)
    if not safe_path:
        raise FileNotFoundError(f"Atom not found: {atom_filename}")
    target_rel = f"2-atoms/{os.path.basename(safe_path)}"

    merkle = compute_vault_merkle(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
    leaves = merkle["leaves"]
    idx = next((i for i, leaf in enumerate(leaves) if leaf["rel_path"] == target_rel), None)
    if idx is None:
        raise PermissionError(f"Atom '{atom_filename}' is not an accepted member of the verified vault Merkle tree.")

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
    return {
        "schema_version": 1,
        "rel_path": target_rel,
        "file_sha256": leaves[idx]["file_sha256"],
        "leaf_sha256": leaves[idx]["leaf_sha256"],
        "siblings": siblings,
        "merkle_root": merkle["merkle_root"],
        "ledger_head_entry_sha256": ledger["head_entry_sha256"] if ledger["synced_with_head"] else None,
    }


def verify_merkle_proof(proof: dict[str, Any]) -> bool:
    """Offline stateless verification of a Merkle inclusion proof bundle."""
    rel_path = str(proof.get("rel_path") or "")
    file_sha256 = str(proof.get("file_sha256") or "")
    leaf_sha256 = str(proof.get("leaf_sha256") or "")
    expected_root = str(proof.get("merkle_root") or "")
    siblings = proof.get("siblings") or []
    if not rel_path or not file_sha256 or not expected_root:
        return False
    if _leaf_digest(rel_path, file_sha256) != leaf_sha256:
        return False

    cur = leaf_sha256
    for step in siblings:
        if not isinstance(step, dict):
            return False
        pos = step.get("position")
        sib = str(step.get("sha256") or "")
        if len(sib) != 64:
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

    p_prove = sub.add_parser("prove", help="Generate offline Merkle proof for an accepted atom")
    p_prove.add_argument("atom", help="Atom filename under 2-atoms/")
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
        res = prove_atom_membership(args.atom)
        print(json.dumps(res, indent=2) if args.json else f"Proof for {res['rel_path']} root={res['merkle_root'][:16]}... valid={verify_merkle_proof(res)}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
