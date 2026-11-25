# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Higher-Order Continuous Distillation: Threads -> Principles -> Mental Models (Epic 2D.2).

Enforces Three-Store & Owner-CAS invariants:
- Night Shift / automated distillers MAY ONLY propose ``status: draft`` artifacts into
  ``brain/7-principles/`` and ``brain/8-mental-models/``.
- Principles require ``>= 3`` live approved backing atoms with pinned ``source_atom_sha256s``.
- Mental Models require ``>= 2`` live approved backing atoms with pinned ``source_atom_sha256s``
  and structured causal mechanism edges (``causes``, ``prevents``, ``enables``, ``depends_on``).
- Only Owner CAS promotion under ``writer.lock`` logged to
  ``0-raw/archive/distillation-decisions.jsonl`` elevates an artifact to ``status: active``.
- If any backing atom is later edited, superseded, rejected, or deleted, verification marks the
  distilled artifact stale (``valid=False``) so ungrounded abstractions never enter planning.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("cognitive_distiller", "scripts.cognitive_distiller"):
    sys.modules.setdefault("cognitive_distiller", sys.modules[__name__])
    sys.modules.setdefault("scripts.cognitive_distiller", sys.modules[__name__])

from atom_mutator import (  # noqa: E402
    atomic_write_bytes,
    content_sha256,
    validate_actor_name,
    vault_writer_lock,
)
from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from memory_format import render_memory_document, set_scalar_field, split_frontmatter  # noqa: E402
from memory_review import parse_atom_record_text, parse_frontmatter_values, parse_scalar_list, section_text  # noqa: E402
from recall import project_tags_match  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

MIN_PRINCIPLE_BACKING_ATOMS = 3
MIN_MENTAL_MODEL_BACKING_ATOMS = 2
VALID_CAUSAL_RELATIONS = frozenset({"causes", "prevents", "enables", "depends_on", "mitigates"})
GENESIS_HASH = "0" * 64


class DistillationError(ValueError):
    """Raised when a Principle or Mental Model violates grounding or CAS rules."""


@dataclass(frozen=True)
class DistilledIntegrityReport:
    path: str
    kind: str
    status: str
    valid: bool
    reason: str
    sha256: str
    title: str
    source_atoms: tuple[str, ...]
    projects: tuple[str, ...]


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (cleaned or "distilled-item")[:64]


def _distillation_log_path(brain_dir: str | Path) -> Path:
    return Path(brain_dir) / "0-raw" / "archive" / "distillation-decisions.jsonl"


def _verify_live_backing_atoms(
    atom_filenames: list[str] | tuple[str, ...],
    *,
    brain_dir: str,
    atoms_dir: str,
    min_required: int,
    pinned_hashes: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    unique_names = []
    for name in atom_filenames:
        clean = str(name or "").strip()
        if clean and clean not in unique_names:
            unique_names.append(clean)
    if len(unique_names) < min_required:
        raise DistillationError(
            f"At least {min_required} distinct approved backing atoms are required (got {len(unique_names)})."
        )

    raw_apath = Path(os.path.abspath(atoms_dir))
    if not os.path.lexists(str(raw_apath)) or _is_reparse_or_link(str(raw_apath)) or not raw_apath.is_dir():
        raise DistillationError("Backing atoms directory is missing or symlinked.")
    apath = raw_apath.resolve()
    sha_map: dict[str, str] = {}
    projects: list[str] = []
    for fname in unique_names:
        if "/" in fname or "\\" in fname or ".." in fname or "\x00" in fname:
            raise DistillationError(f"Invalid backing atom filename: {fname!r}")
        raw_cand = raw_apath / fname
        if not os.path.lexists(str(raw_cand)) or _is_reparse_or_link(str(raw_cand)):
            raise DistillationError(f"Backing atom missing or symlinked: {fname}")
        cand = raw_cand.resolve()
        if cand.parent != apath or not cand.is_file() or _is_reparse_or_link(str(cand)):
            raise DistillationError(f"Backing atom missing or symlinked: {fname}")
        raw_bytes = cand.read_bytes()
        rec = parse_atom_record_text(fname, raw_bytes.decode("utf-8"))
        if (
            rec.get("review_status") != "accepted"
            or rec.get("claim_state") != "human_reviewed"
            or rec.get("superseded_by")
            or not approved_revision_is_current(str(cand), brain_dir=brain_dir, atoms_dir=atoms_dir)
        ):
            raise DistillationError(f"Backing atom '{fname}' is not currently approved/current.")
        ep_label = str(rec.get("epistemic_label") or "claim").strip().lower()
        ep_contract = str(rec.get("epistemic_contract") or "").strip()
        if ep_label == "preference" or bool(ep_contract):
            raise DistillationError(f"Preference atom '{fname}' cannot back a distilled principle or mental model.")
        live_sha = content_sha256(raw_bytes)
        if pinned_hashes is not None:
            expected = str(pinned_hashes.get(fname) or "").strip()
            if expected != live_sha:
                raise DistillationError(f"Backing atom '{fname}' hash drifted from pinned revision.")
        sha_map[fname] = live_sha
        for ctx in rec.get("contexts") or ():
            if str(ctx).lower().startswith("project:") and str(ctx) not in projects:
                projects.append(str(ctx))
    return sha_map, projects


def _refuse_active_artifact_overwrite(out_path: Path, sub_field: str) -> None:
    if not os.path.lexists(str(out_path)):
        return
    if _is_reparse_or_link(str(out_path)):
        raise PermissionError(f"Refusing symlinked distilled artifact path: {out_path}")
    if not out_path.is_file():
        return
    try:
        existing_text = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DistillationError(f"Unable to inspect existing distilled artifact: {out_path.name}") from exc
    parsed = split_frontmatter(existing_text)
    if parsed is not None:
        fm_vals = parse_frontmatter_values(parsed[0])
        st = str(fm_vals.get("status") or "").strip().lower()
        sub_st = str(fm_vals.get(sub_field) or "").strip().lower()
        if st == "active" or sub_st == "active":
            raise DistillationError(
                f"Refusing to overwrite existing active distilled artifact '{out_path.name}'; revoke via Owner CAS first."
            )


def _canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _read_distillation_log(log_path: Path) -> tuple[bool, list[dict[str, Any]], str]:
    if not log_path.exists():
        return True, [], GENESIS_HASH
    if _is_reparse_or_link(str(log_path)):
        return False, [], ""
    try:
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError):
        return False, [], ""
    records: list[dict[str, Any]] = []
    expected_prev = GENESIS_HASH
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            return False, [], ""
        if not isinstance(obj, dict):
            return False, [], ""
        rec_hash = str(obj.get("entry_sha256") or "")
        body = {k: v for k, v in obj.items() if k != "entry_sha256"}
        if str(body.get("prev_entry_sha256") or "") != expected_prev:
            return False, [], ""
        if hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest() != rec_hash:
            return False, [], ""
        records.append(obj)
        expected_prev = rec_hash
    return True, records, expected_prev


def _append_distillation_decision(
    log_path: Path,
    *,
    artifact_rel: str,
    kind: str,
    action: str,
    actor: str,
    expected_sha256: str,
    new_sha256: str,
    reason: str = "",
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ok, records, prev_hash = _read_distillation_log(log_path)
    if not ok:
        raise DistillationError("Distillation decision log failed hash-chain verification.")
    body = {
        "seq": len(records) + 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "artifact_rel": artifact_rel,
        "kind": kind,
        "action": action,
        "actor": actor,
        "expected_sha256": expected_sha256,
        "new_sha256": new_sha256,
        "reason": reason,
        "prev_entry_sha256": prev_hash,
    }
    entry_sha = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    record = {**body, "entry_sha256": entry_sha}
    existing = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    new_content = existing + json.dumps(record, ensure_ascii=False) + "\n"
    atomic_write_bytes(str(log_path), new_content.encode("utf-8"))
    return record


def propose_principle(
    *,
    title: str,
    statement: str,
    backing_atoms: list[str],
    source_threads: list[str] | None = None,
    bounds: str = "",
    filename: str | None = None,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a status=draft Principle anchored to >=3 approved backing atoms."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    clean_title = (title or "").strip()
    clean_stmt = (statement or "").strip()
    if not clean_title or not clean_stmt:
        raise DistillationError("Principle title and statement are required.")
    sha_map, projects = _verify_live_backing_atoms(
        backing_atoms,
        brain_dir=resolved_brain,
        atoms_dir=resolved_atoms,
        min_required=MIN_PRINCIPLE_BACKING_ATOMS,
    )
    pdir = Path(resolved_brain) / "7-principles"
    if os.path.lexists(str(pdir)) and _is_reparse_or_link(str(pdir)):
        raise PermissionError(f"Refusing symlinked principles directory: {pdir}")
    slug = _slugify(filename[:-3] if filename and filename.endswith(".md") else clean_title)
    out_path = pdir / f"{slug}.md"

    today_str = datetime.date.today().isoformat()
    fm_lines = [
        f"id: {json.dumps(slug)}",
        'type: "principle"',
        f"title: {json.dumps(clean_title, ensure_ascii=False)}",
        'status: "draft"',
        'principle_status: "draft"',
        f"source_atoms: {json.dumps(list(sha_map.keys()), ensure_ascii=False)}",
        f"source_atom_sha256s: {json.dumps(sha_map, ensure_ascii=False, sort_keys=True)}",
        f"source_threads: {json.dumps(list(source_threads or []), ensure_ascii=False)}",
        f"projects: {json.dumps(projects, ensure_ascii=False)}",
        f"created: {json.dumps(today_str)}",
    ]
    body_lines = [
        "## Statement",
        clean_stmt,
        "",
        "## Evidence Summary",
        *[f"- [[{atom_fn}]] (`{h[:12]}`)" for atom_fn, h in sha_map.items()],
        "",
        "## Counter-Examples & Bounds",
        bounds.strip() or "Requires human owner verification before promotion.",
    ]
    raw_bytes = render_memory_document(fm_lines, body_lines).encode("utf-8")
    with vault_writer_lock(resolved_brain):
        if os.path.lexists(str(pdir)) and _is_reparse_or_link(str(pdir)):
            raise PermissionError(f"Refusing symlinked principles directory: {pdir}")
        pdir.mkdir(parents=True, exist_ok=True)
        _refuse_active_artifact_overwrite(out_path, "principle_status")
        atomic_write_bytes(str(out_path), raw_bytes)
    return {
        "status": "draft",
        "kind": "principle",
        "path": str(out_path),
        "filename": out_path.name,
        "sha256": content_sha256(raw_bytes),
        "backing_atoms": list(sha_map.keys()),
        "projects": projects,
    }


def propose_mental_model(
    *,
    title: str,
    summary: str,
    edges: list[dict[str, str]],
    backing_atoms: list[str],
    filename: str | None = None,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create a status=draft Causal Mental Model anchored to approved backing atoms."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    clean_title = (title or "").strip()
    clean_summary = (summary or "").strip()
    if not clean_title or not clean_summary:
        raise DistillationError("Mental model title and summary are required.")
    clean_edges: list[dict[str, str]] = []
    for ed in edges or ():
        src = str(ed.get("from") or "").strip()
        rel = str(ed.get("relation") or "").strip().lower()
        dst = str(ed.get("to") or "").strip()
        if not src or not dst or rel not in VALID_CAUSAL_RELATIONS:
            raise DistillationError(
                f"Invalid causal edge {ed!r}; relation must be one of {sorted(VALID_CAUSAL_RELATIONS)}."
            )
        clean_edges.append(
            {
                "from": src,
                "relation": rel,
                "to": dst,
                "evidence_atom": str(ed.get("evidence_atom") or "").strip(),
            }
        )
    if not clean_edges:
        raise DistillationError("Mental model requires at least 1 structured causal edge.")

    sha_map, projects = _verify_live_backing_atoms(
        backing_atoms,
        brain_dir=resolved_brain,
        atoms_dir=resolved_atoms,
        min_required=MIN_MENTAL_MODEL_BACKING_ATOMS,
    )
    mdir = Path(resolved_brain) / "8-mental-models"
    if os.path.lexists(str(mdir)) and _is_reparse_or_link(str(mdir)):
        raise PermissionError(f"Refusing symlinked mental models directory: {mdir}")
    slug = _slugify(filename[:-3] if filename and filename.endswith(".md") else clean_title)
    out_path = mdir / f"{slug}.md"

    today_str = datetime.date.today().isoformat()
    fm_lines = [
        f"id: {json.dumps(slug)}",
        'type: "mental_model"',
        f"title: {json.dumps(clean_title, ensure_ascii=False)}",
        'status: "draft"',
        'model_status: "draft"',
        f"source_atoms: {json.dumps(list(sha_map.keys()), ensure_ascii=False)}",
        f"source_atom_sha256s: {json.dumps(sha_map, ensure_ascii=False, sort_keys=True)}",
        f"edges_json: {json.dumps(clean_edges, ensure_ascii=False)}",
        f"projects: {json.dumps(projects, ensure_ascii=False)}",
        f"created: {json.dumps(today_str)}",
    ]
    body_lines = [
        "## Mechanism Overview",
        clean_summary,
        "",
        "## Causal Chain",
        *[f"- `{e['from']}` --[{e['relation']}]--> `{e['to']}` ({e.get('evidence_atom') or 'cited'})" for e in clean_edges],
        "",
        "## Backing Atoms",
        *[f"- [[{atom_fn}]] (`{h[:12]}`)" for atom_fn, h in sha_map.items()],
    ]
    raw_bytes = render_memory_document(fm_lines, body_lines).encode("utf-8")
    with vault_writer_lock(resolved_brain):
        if os.path.lexists(str(mdir)) and _is_reparse_or_link(str(mdir)):
            raise PermissionError(f"Refusing symlinked mental models directory: {mdir}")
        mdir.mkdir(parents=True, exist_ok=True)
        _refuse_active_artifact_overwrite(out_path, "model_status")
        atomic_write_bytes(str(out_path), raw_bytes)
    return {
        "status": "draft",
        "kind": "mental_model",
        "path": str(out_path),
        "filename": out_path.name,
        "sha256": content_sha256(raw_bytes),
        "backing_atoms": list(sha_map.keys()),
        "edges": clean_edges,
        "projects": projects,
    }


def _parse_json_or_list(raw_val: Any) -> Any:
    if isinstance(raw_val, (list, dict)):
        return raw_val
    if isinstance(raw_val, str):
        txt = raw_val.strip()
        if txt.startswith("{") or txt.startswith("["):
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                pass
        return parse_scalar_list(txt)
    return []


def promote_distilled_artifact(
    artifact_path: str | Path,
    *,
    expected_sha256: str,
    actor: str = "owner",
    action: str = "promote",
    reason: str = "",
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Owner CAS transition for 7-principles/*.md and 8-mental-models/*.md."""
    clean_actor = validate_actor_name(actor)
    clean_action = (action or "").strip().lower()
    if clean_action not in {"promote", "revoke"}:
        raise DistillationError("action must be 'promote' or 'revoke'.")
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain).resolve()
    raw_target = Path(os.path.abspath(str(artifact_path)))
    if (
        not os.path.lexists(str(raw_target))
        or _is_reparse_or_link(str(raw_target))
        or _is_reparse_or_link(str(raw_target.parent))
    ):
        raise DistillationError(f"Distilled artifact not found or symlinked: {artifact_path}")
    target = raw_target.resolve()
    if _is_reparse_or_link(str(target)) or not target.is_file():
        raise DistillationError(f"Distilled artifact not found or symlinked: {artifact_path}")
    try:
        rel_posix = target.relative_to(bpath).as_posix()
    except ValueError as exc:
        raise DistillationError("Distilled artifact must reside inside brain directory.") from exc
    if rel_posix.startswith("7-principles/"):
        kind = "principle"
        sub_field = "principle_status"
        min_req = MIN_PRINCIPLE_BACKING_ATOMS
    elif rel_posix.startswith("8-mental-models/"):
        kind = "mental_model"
        sub_field = "model_status"
        min_req = MIN_MENTAL_MODEL_BACKING_ATOMS
    else:
        raise DistillationError(f"Artifact path must live in 7-principles/ or 8-mental-models/: {rel_posix}")

    with vault_writer_lock(resolved_brain):
        raw_bytes = target.read_bytes()
        current_sha = content_sha256(raw_bytes)
        if current_sha != (expected_sha256 or "").strip().lower():
            raise DistillationError(
                f"CAS mismatch on {rel_posix}: expected {expected_sha256}, actual {current_sha}."
            )
        text = raw_bytes.decode("utf-8")
        parsed = split_frontmatter(text)
        if parsed is None:
            raise DistillationError(f"Missing frontmatter in {rel_posix}")
        fm_lines, body_lines = parsed
        fm_vals = parse_frontmatter_values(fm_lines)
        src_atoms = _parse_json_or_list(fm_vals.get("source_atoms"))
        pinned = _parse_json_or_list(fm_vals.get("source_atom_sha256s"))
        if not isinstance(pinned, dict):
            raise DistillationError(f"Missing source_atom_sha256s map in {rel_posix}")

        new_status = "active" if clean_action == "promote" else "revoked"
        if new_status == "active":
            _verify_live_backing_atoms(
                list(src_atoms),
                brain_dir=resolved_brain,
                atoms_dir=resolved_atoms,
                min_required=min_req,
                pinned_hashes=pinned,
            )

        updated_fm = set_scalar_field(fm_lines, "status", json.dumps(new_status), "type")
        updated_fm = set_scalar_field(updated_fm, sub_field, json.dumps(new_status), "status")
        if new_status == "active":
            now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
            updated_fm = set_scalar_field(updated_fm, "promoted_by", json.dumps(clean_actor), sub_field)
            updated_fm = set_scalar_field(updated_fm, "promoted_at", json.dumps(now_iso), "promoted_by")

        new_bytes = render_memory_document(updated_fm, body_lines).encode("utf-8")
        new_sha = content_sha256(new_bytes)
        atomic_write_bytes(str(target), new_bytes)
        dec = _append_distillation_decision(
            _distillation_log_path(resolved_brain),
            artifact_rel=rel_posix,
            kind=kind,
            action=f"{clean_action}_{kind}",
            actor=clean_actor,
            expected_sha256=current_sha,
            new_sha256=new_sha,
            reason=reason,
        )
    return {
        "status": new_status,
        "kind": kind,
        "rel_path": rel_posix,
        "sha256": new_sha,
        "decision": dec,
    }


def verify_distilled_artifact_integrity(
    artifact_path: str | Path,
    *,
    kind: str = "auto",
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> DistilledIntegrityReport:
    """Verify Owner CAS decision chain and live backing atom hashes for a distilled file."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain).resolve()
    raw_target = Path(os.path.abspath(str(artifact_path)))
    if (
        not os.path.lexists(str(raw_target))
        or _is_reparse_or_link(str(raw_target))
        or _is_reparse_or_link(str(raw_target.parent))
    ):
        return DistilledIntegrityReport(str(artifact_path), kind, "missing", False, "missing_or_symlinked", "", "", (), ())
    target = raw_target.resolve()
    if _is_reparse_or_link(str(target)) or not target.is_file():
        return DistilledIntegrityReport(str(artifact_path), kind, "missing", False, "missing_or_symlinked", "", "", (), ())
    try:
        rel_posix = target.relative_to(bpath).as_posix()
    except ValueError:
        return DistilledIntegrityReport(str(target), kind, "outside_vault", False, "outside_vault", "", "", (), ())

    eff_kind = kind
    if eff_kind == "auto":
        eff_kind = "principle" if rel_posix.startswith("7-principles/") else "mental_model"
    min_req = MIN_PRINCIPLE_BACKING_ATOMS if eff_kind == "principle" else MIN_MENTAL_MODEL_BACKING_ATOMS

    raw_bytes = target.read_bytes()
    fhash = content_sha256(raw_bytes)
    parsed = split_frontmatter(raw_bytes.decode("utf-8", errors="replace"))
    if parsed is None:
        return DistilledIntegrityReport(str(target), eff_kind, "invalid", False, "missing_frontmatter", fhash, target.stem, (), ())
    fm_vals = parse_frontmatter_values(parsed[0])
    status = str(fm_vals.get("status") or "draft").strip().lower()
    title = str(fm_vals.get("title") or target.stem).strip()
    src_atoms = tuple(str(x) for x in (_parse_json_or_list(fm_vals.get("source_atoms")) or []))
    pinned = _parse_json_or_list(fm_vals.get("source_atom_sha256s"))
    projects = tuple(str(x) for x in (_parse_json_or_list(fm_vals.get("projects")) or []))

    if status != "active":
        return DistilledIntegrityReport(str(target), eff_kind, status, True, "non_active_draft_or_revoked", fhash, title, src_atoms, projects)

    log_ok, records, _ = _read_distillation_log(_distillation_log_path(resolved_brain))
    if not log_ok:
        return DistilledIntegrityReport(str(target), eff_kind, status, False, "corrupt_distillation_log", fhash, title, src_atoms, projects)
    matching = [r for r in records if r.get("artifact_rel") == rel_posix]
    if not matching or matching[-1].get("action") != f"promote_{eff_kind}" or matching[-1].get("new_sha256") != fhash:
        return DistilledIntegrityReport(str(target), eff_kind, status, False, "missing_owner_cas_promotion", fhash, title, src_atoms, projects)

    if not isinstance(pinned, dict):
        return DistilledIntegrityReport(str(target), eff_kind, status, False, "missing_pinned_atom_hashes", fhash, title, src_atoms, projects)
    try:
        _verify_live_backing_atoms(
            src_atoms,
            brain_dir=resolved_brain,
            atoms_dir=resolved_atoms,
            min_required=min_req,
            pinned_hashes=pinned,
        )
    except DistillationError:
        return DistilledIntegrityReport(str(target), eff_kind, status, False, "backing_atom_drifted", fhash, title, src_atoms, projects)

    return DistilledIntegrityReport(str(target), eff_kind, "active", True, "verified_active", fhash, title, src_atoms, projects)


def list_distilled_artifacts(
    *,
    kind: str = "principle",
    project: str | None = None,
    active_only: bool = True,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    subdir = "7-principles" if kind == "principle" else "8-mental-models"
    root = Path(resolved_brain) / subdir
    if not root.is_dir() or _is_reparse_or_link(str(root)):
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not is_content_markdown(entry.name):
            continue
        raw_entry = root / entry.name
        if not os.path.lexists(str(raw_entry)) or _is_reparse_or_link(str(raw_entry)):
            continue
        rep = verify_distilled_artifact_integrity(raw_entry, kind=kind, brain_dir=resolved_brain, atoms_dir=resolved_atoms)
        if not rep.valid and rep.reason in {"missing_or_symlinked", "outside_vault"}:
            continue
        if active_only and (not rep.valid or rep.status != "active"):
            continue
        if project:
            if not rep.projects or not all(bool(project_tags_match((p,), (project,))) for p in rep.projects):
                continue
        if _is_reparse_or_link(rep.path) or not Path(rep.path).is_file():
            continue
        text = Path(rep.path).read_text(encoding="utf-8", errors="replace")
        parsed = split_frontmatter(text)
        body = "\n".join(parsed[1]) if parsed else text
        fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
        items.append(
            {
                "filename": entry.name,
                "path": rep.path,
                "kind": rep.kind,
                "status": rep.status,
                "valid": rep.valid,
                "reason": rep.reason,
                "sha256": rep.sha256,
                "title": rep.title,
                "statement": section_text(body, "Statement") or section_text(body, "Mechanism Overview"),
                "source_atoms": list(rep.source_atoms),
                "projects": list(rep.projects),
                "edges": _parse_json_or_list(fm_vals.get("edges_json")) if kind == "mental_model" else [],
            }
        )
    return items


def distill_cross_project_candidates(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    min_atoms_per_cluster: int = MIN_PRINCIPLE_BACKING_ATOMS,
) -> dict[str, Any]:
    """Scan currently approved atoms and stage draft Principles & Mental Models by shared context/tag."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    apath = Path(resolved_atoms)
    if not apath.is_dir() or _is_reparse_or_link(str(apath)):
        return {"principles_created": [], "mental_models_created": []}

    clusters: dict[str, list[dict[str, Any]]] = {}
    for entry in sorted(os.scandir(apath), key=lambda e: e.name):
        if not is_content_markdown(entry.name):
            continue
        p = apath / entry.name
        if not p.is_file() or _is_reparse_or_link(str(p)):
            continue
        try:
            raw = p.read_text(encoding="utf-8")
            rec = parse_atom_record_text(entry.name, raw)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if rec.get("review_status") != "accepted" or rec.get("superseded_by"):
            continue
        if not approved_revision_is_current(str(p), brain_dir=resolved_brain, atoms_dir=resolved_atoms):
            continue
        ep_label = str(rec.get("epistemic_label") or "claim").strip().lower()
        ep_contract = str(rec.get("epistemic_contract") or "").strip()
        if ep_label == "preference" or bool(ep_contract):
            continue
        parsed = split_frontmatter(raw)
        fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
        tags = parse_scalar_list(fm_vals.get("tags", [])) if isinstance(fm_vals.get("tags"), str) else list(fm_vals.get("tags") or [])
        topic_keys = [str(t).strip().lower() for t in tags if str(t).strip() and not str(t).lower().startswith("project:")]
        if not topic_keys:
            topic_keys = [str(c).strip().lower() for c in (rec.get("contexts") or []) if not str(c).lower().startswith("project:")]
        for k in topic_keys[:2] or ["general"]:
            clusters.setdefault(k, []).append(rec)

    created_principles = []
    for topic, recs in sorted(clusters.items()):
        if len(recs) < max(MIN_PRINCIPLE_BACKING_ATOMS, int(min_atoms_per_cluster)):
            continue
        projs = {c for r in recs for c in (r.get("contexts") or []) if str(c).lower().startswith("project:")}
        if len(projs) >= 1:
            fn = f"principle-{_slugify(topic)}.md"
            if not (Path(resolved_brain) / "7-principles" / fn).exists():
                stmt = " Across projects, verified evidence indicates: " + "; ".join(
                    str(r.get("claim") or "").strip().rstrip(".") for r in recs[:4] if r.get("claim")
                ) + "."
                res = propose_principle(
                    title=f"Cross-Project Principle: {topic}",
                    statement=stmt.strip(),
                    backing_atoms=[str(r["filename"]) for r in recs[:6]],
                    filename=fn,
                    brain_dir=resolved_brain,
                    atoms_dir=resolved_atoms,
                )
                created_principles.append(res)

    return {"principles_created": created_principles, "mental_models_created": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Higher-Order Distillation (Principles & Mental Models)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_dist = sub.add_parser("distill", help="Propose draft Principles from verified atoms")
    p_dist.add_argument("--json", action="store_true")

    p_ls = sub.add_parser("list", help="List Principles or Mental Models")
    p_ls.add_argument("--kind", choices=("principle", "mental_model"), default="principle")
    p_ls.add_argument("--project", default=None)
    p_ls.add_argument("--all", action="store_true")
    p_ls.add_argument("--json", action="store_true")

    p_prom = sub.add_parser("promote", help="Owner CAS promote or revoke a draft Principle/Mental Model")
    p_prom.add_argument("path")
    p_prom.add_argument("--expected-sha256", required=True)
    p_prom.add_argument("--action", choices=("promote", "revoke"), default="promote")
    p_prom.add_argument("--actor", default="owner")
    p_prom.add_argument("--reason", default="")
    p_prom.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "distill":
        out = distill_cross_project_candidates()
        print(json.dumps(out, indent=2) if args.json else f"principles={len(out['principles_created'])}")
        return 0
    if args.cmd == "list":
        rows = list_distilled_artifacts(kind=args.kind, project=args.project, active_only=not args.all)
        print(json.dumps(rows, indent=2) if args.json else "\n".join(f"{r['filename']} [{r['status']}] {r['title']}" for r in rows))
        return 0
    out = promote_distilled_artifact(
        args.path,
        expected_sha256=args.expected_sha256,
        actor=args.actor,
        action=args.action,
        reason=args.reason,
    )
    print(json.dumps(out, indent=2) if args.json else f"{out['kind']} {out['status']} sha256={out['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
