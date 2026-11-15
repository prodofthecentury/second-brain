# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Time-Travel Vault Snapshots, Semantic Diff & Point-in-Time Recall.

Creates immutable, content-addressed checkpoints under brain/.sidecar/snapshots/
recording the exact SHA-256 hashes, claims, and contexts of all owner-approved
atoms and playbooks at timestamp T. Supports:
  * Semantic diff between any two snapshots (or snapshot vs live vault)
  * Point-in-time recall ("what did my verified brain know at snapshot T?")
    with zero leakage from later edits or unapproved drafts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current, content_sha256, review_lock  # noqa: E402
from config import ATOMS_DIR, BRAIN_DIR, _is_reparse_or_link, is_content_markdown, safe_file_in_dir  # noqa: E402
from memory_review import parse_atom_record  # noqa: E402
from recall import normalize_project_tag, project_tags_match  # noqa: E402
from skill_playbooks import verify_playbook_integrity  # noqa: E402
from vault_ledger import _build_merkle_levels, _leaf_digest  # noqa: E402

_SNAP_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_WORD_RE = re.compile(r"[a-z0-9_]{2,}")


def _snapshots_dir(brain_dir: str) -> Path:
    return Path(brain_dir) / ".sidecar" / "snapshots"


def _capture_live_state(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    pb_dir = playbooks_dir if playbooks_dir is not None else os.path.join(brain_dir, "5-playbooks")
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
                "contexts": list(rec.get("contexts") or []),
                "claim": rec.get("claim") or "",
            }

    playbooks: dict[str, dict[str, Any]] = {}
    if os.path.isdir(pb_dir) and not _is_reparse_or_link(pb_dir):
        telemetry_db = os.path.join(brain_dir, ".sidecar", "telemetry.sqlite3")
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
                "filename": pb_rep.filename or entry.name,
                "file_sha256": fhash,
                "contexts": list(pb_rep.contexts),
            }

    leaves.sort(key=lambda item: item[0])
    merkle_root = _build_merkle_levels([leaf_hash for _, leaf_hash in leaves])[-1][0]
    return {
        "merkle_root": merkle_root,
        "atoms": atoms,
        "playbooks": playbooks,
    }


def create_snapshot(
    *,
    label: str = "",
    actor: str = "owner",
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Create an immutable point-in-time snapshot under brain/.sidecar/snapshots/."""
    clean_actor = (actor or "").strip()
    if not clean_actor:
        raise ValueError("Snapshot requires a non-empty actor.")

    snap_dir = _snapshots_dir(brain_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(snap_dir.parent)) or _is_reparse_or_link(str(snap_dir)):
        raise PermissionError("Refusing to write snapshot through a symlink or junction.")

    with review_lock(str(snap_dir.parent / "writer.lock")):
        if _is_reparse_or_link(str(snap_dir.parent)) or _is_reparse_or_link(str(snap_dir)):
            raise PermissionError("Refusing to write snapshot through a symlink or junction.")
        state = _capture_live_state(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        while True:
            entropy = secrets.token_hex(8)
            nonce = hashlib.sha256(f"{stamp}:{state['merkle_root']}:{label}:{entropy}".encode("utf-8")).hexdigest()[:8]
            snapshot_id = f"snap-{stamp}-{nonce}"
            target = snap_dir / f"{snapshot_id}.json"
            if not target.exists():
                break

        doc: dict[str, Any] = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "created_at": now.isoformat(timespec="seconds"),
            "actor": clean_actor,
            "label": (label or "").strip()[:160],
            "merkle_root": state["merkle_root"],
            "atom_count": len(state["atoms"]),
            "playbook_count": len(state["playbooks"]),
            "atoms": state["atoms"],
            "playbooks": state["playbooks"],
        }
        fd, tmp_path = tempfile.mkstemp(dir=str(snap_dir), prefix=".snap-", suffix=".tmp")
        try:
            os.write(fd, json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, target)
        return doc


def list_snapshots(*, brain_dir: str = BRAIN_DIR) -> list[dict[str, Any]]:
    snap_dir = _snapshots_dir(brain_dir)
    if not snap_dir.is_dir() or _is_reparse_or_link(str(snap_dir.parent)) or _is_reparse_or_link(str(snap_dir)):
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(os.scandir(snap_dir), key=lambda e: e.name, reverse=True):
        if not entry.name.endswith(".json") or _is_reparse_or_link(entry.path):
            continue
        try:
            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
            out.append(
                {
                    "snapshot_id": data["snapshot_id"],
                    "created_at": data["created_at"],
                    "label": data.get("label", ""),
                    "actor": data.get("actor", ""),
                    "merkle_root": data.get("merkle_root", ""),
                    "atom_count": data.get("atom_count", 0),
                    "playbook_count": data.get("playbook_count", 0),
                }
            )
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out


def load_snapshot(snapshot_id: str, *, brain_dir: str = BRAIN_DIR) -> dict[str, Any]:
    clean = (snapshot_id or "").strip()
    if clean.endswith(".json"):
        clean = clean[:-5]
    if not clean or not _SNAP_ID_RE.match(clean):
        raise ValueError(f"Invalid snapshot_id: {snapshot_id!r}")
    snap_dir = _snapshots_dir(brain_dir)
    if _is_reparse_or_link(str(snap_dir.parent)) or _is_reparse_or_link(str(snap_dir)):
        raise PermissionError("Refusing to load snapshot through a symlink or junction.")
    safe = safe_file_in_dir(str(snap_dir), f"{clean}.json", allowed_exts={".json"}, must_exist=True)
    if not safe:
        raise FileNotFoundError(f"Snapshot not found: {clean}")
    if _is_reparse_or_link(safe):
        raise PermissionError("Refusing to load snapshot through a symlink or junction.")
    return json.loads(Path(safe).read_text(encoding="utf-8"))


def diff_snapshots(
    from_snapshot_id: str,
    to_snapshot_id_or_live: str = "live",
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    playbooks_dir: str | None = None,
) -> dict[str, Any]:
    """Compute a semantic diff between two snapshots (or snapshot vs live vault)."""
    before = load_snapshot(from_snapshot_id, brain_dir=brain_dir)
    if to_snapshot_id_or_live.strip().lower() == "live":
        after_state = _capture_live_state(brain_dir=brain_dir, atoms_dir=atoms_dir, playbooks_dir=playbooks_dir)
        after_id = "live"
    else:
        after_doc = load_snapshot(to_snapshot_id_or_live, brain_dir=brain_dir)
        after_state = {
            "merkle_root": after_doc["merkle_root"],
            "atoms": after_doc["atoms"],
            "playbooks": after_doc["playbooks"],
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
    }


def recall_at_snapshot(
    query: str,
    snapshot_id: str,
    *,
    project_scope: str | None = None,
    include_preferences: bool = False,
    top_k: int = 5,
    brain_dir: str = BRAIN_DIR,
) -> dict[str, Any]:
    """Deterministic point-in-time recall over only the atoms approved in snapshot_id."""
    snap = load_snapshot(snapshot_id, brain_dir=brain_dir)
    q_tokens = set(_WORD_RE.findall((query or "").lower()))
    if not q_tokens:
        return {"snapshot_id": snap["snapshot_id"], "results": []}

    target = normalize_project_tag(project_scope) if (project_scope and project_scope.strip()) else ""

    scored: list[tuple[float, dict[str, Any]]] = []
    for fname, atom in snap.get("atoms", {}).items():
        if not include_preferences and (
            atom.get("epistemic_label") == "preference" or bool(atom.get("epistemic_contract"))
        ):
            continue
        contexts = atom.get("contexts") or []
        if target:
            proj_tags = [str(c).strip() for c in contexts if str(c).strip().lower().startswith("project:")]
            if not proj_tags or not all(bool(project_tags_match((c,), (target,))) for c in proj_tags):
                continue
        claim = str(atom.get("claim") or "")
        c_tokens = set(_WORD_RE.findall(claim.lower()))
        overlap = len(q_tokens & c_tokens)
        if overlap == 0:
            continue
        score = overlap / max(1, len(q_tokens))
        scored.append(
            (
                score,
                {
                    "filename": fname,
                    "atom_id": atom.get("atom_id", ""),
                    "claim": claim,
                    "contexts": contexts,
                    "file_sha256": atom.get("file_sha256", ""),
                    "score": round(score, 4),
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1]["filename"]))
    return {
        "snapshot_id": snap["snapshot_id"],
        "created_at": snap["created_at"],
        "merkle_root": snap["merkle_root"],
        "results": [item[1] for item in scored[: max(1, min(top_k, 25))]],
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

    p_recall = sub.add_parser("recall", help="Recall only verified atoms as of snapshot_id")
    p_recall.add_argument("snapshot_id")
    p_recall.add_argument("query")
    p_recall.add_argument("--project", default=None)
    p_recall.add_argument("--json", action="store_true")

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
        print(json.dumps(res, indent=2) if args.json else f"added={len(res['added_atoms'])} removed={len(res['removed_atoms'])} modified={len(res['modified_atoms'])}")
        return 0
    if args.cmd == "recall":
        res = recall_at_snapshot(args.query, args.snapshot_id, project_scope=args.project)
        print(json.dumps(res, indent=2) if args.json else f"{len(res['results'])} result(s) at {res['snapshot_id']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
