# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Owner-authorized project scoping migration for canonical vault atoms.

Preserves all byte-anchor, CAS decision-log, lexical, and Merkle ledger guarantees:
1. Dry-run by default: scans all atoms in ``2-atoms/`` and reports current project contexts.
2. Human owner authorization required: mutating approved atoms requires ``--apply``,
   a non-agent human ``--actor <owner>`` (validated via ``owner_gates._validate_human_actor``),
   and either an interactive TTY or explicit ``--confirm-migration``.
3. Canonical integrity preserved: writes ``.history/`` backups, updates ``contexts`` via
   ``memory_format.set_string_list_field`` (preserving multiline YAML), re-verifies exact
   byte-offset evidence anchors, appends attested ``decision-record.jsonl`` entries,
   refreshes ``lexical.sqlite3`` (and active Chroma vectors when compatible), and commits a
   ``vault-ledger.jsonl`` checkpoint. Any benchmark dataset query pinning modified atom
   hashes must still be re-anchored explicitly by the owner afterwards.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


import argparse
import datetime
import os
from pathlib import Path
import sys
from typing import Any

if __name__ in ("owner_scope_migration", "scripts.owner_scope_migration"):
    sys.modules.setdefault("owner_scope_migration", sys.modules[__name__])
    sys.modules.setdefault("scripts.owner_scope_migration", sys.modules[__name__])

from scripts.atom_mutator import (  # noqa: E402
    append_decision_records,
    approved_revision_is_current,
    atomic_write_bytes,
    content_sha256,
    refresh_lexical_index,
    review_lock,
)
from scripts.config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    resolve_module_atoms_and_brain,
    safe_file_in_dir,
    snapshot_module_defaults,
)
from scripts.evidence import EvidenceValidationError, find_evidence_anchor  # noqa: E402
from scripts.memory_format import render_memory_document, set_string_list_field, split_frontmatter  # noqa: E402
from scripts.memory_review import parse_frontmatter_values  # noqa: E402
from scripts.owner_gates import _assert_safe_regular_path, _validate_human_actor  # noqa: E402
from scripts.recall import normalize_project_tag  # noqa: E402
from scripts.vault_ledger import commit_ledger_checkpoint  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())


def scan_atom_scopes(
    atoms_dir: str | Path | None = None,
    *,
    brain_dir: str | Path | None = None,
) -> dict[str, object]:
    """Scan ``2-atoms/`` safely and classify atoms with vs. without project scopes."""
    _, eff_atoms_str = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=str(atoms_dir) if atoms_dir is not None else None,
        brain_dir=str(brain_dir) if brain_dir is not None else None,
    )
    eff_atoms = _assert_safe_regular_path(Path(eff_atoms_str), "atoms directory")
    unscoped: list[str] = []
    scoped: list[dict[str, object]] = []

    for path in sorted(eff_atoms.glob("*.md")):
        if path.name == "README.md" or path.name.startswith(".") or _is_reparse_or_link(str(path)):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = split_frontmatter(content)
        if parsed is None:
            continue
        vals = parse_frontmatter_values(parsed[0])
        raw_ctx = vals.get("contexts", [])
        if isinstance(raw_ctx, str):
            ctx_list = [raw_ctx] if raw_ctx else []
        elif isinstance(raw_ctx, list):
            ctx_list = [str(x) for x in raw_ctx]
        else:
            ctx_list = []
        norm_scopes = [normalize_project_tag(x) for x in ctx_list if normalize_project_tag(x)]
        if norm_scopes:
            scoped.append({"filename": path.name, "scopes": norm_scopes})
        else:
            unscoped.append(path.name)

    return {
        "total_atoms": len(scoped) + len(unscoped),
        "scoped_count": len(scoped),
        "unscoped_count": len(unscoped),
        "scoped": scoped,
        "unscoped": unscoped,
    }


def apply_scope_migration(
    *,
    scope: str,
    actor: str,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    interactive_tty_verified: bool | None = None,
) -> dict[str, Any]:
    """Apply a normalized ``project:<id>`` scope to unscoped canonical atoms while preserving governance."""
    clean_actor = _validate_human_actor(actor)
    tty_ok = (
        bool(sys.stdin.isatty() and sys.stdout.isatty())
        if interactive_tty_verified is None
        else bool(interactive_tty_verified)
    )
    if not tty_ok:
        raise PermissionError(
            "Scope migration mutation requires an interactive human TTY session or --confirm-migration."
        )

    norm_scope = normalize_project_tag(scope)
    if not norm_scope:
        raise ValueError(f"Invalid project scope {scope!r}; expected project:<slug>.")

    eff_brain_str, eff_atoms_str = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=str(atoms_dir) if atoms_dir is not None else None,
        brain_dir=str(brain_dir) if brain_dir is not None else None,
    )
    brain_root = str(_assert_safe_regular_path(Path(eff_brain_str), "vault directory"))
    eff_atoms = str(_assert_safe_regular_path(Path(eff_atoms_str), "atoms directory"))
    lock_path = os.path.join(brain_root, ".sidecar", "writer.lock")
    history_dir = os.path.join(eff_atoms, ".history")
    decision_log_path = os.path.join(brain_root, "0-raw", "archive", "decision-record.jsonl")

    updated_files: list[str] = []
    skipped_unverified: list[str] = []
    prior_bytes_by_path: dict[str, bytes] = {}
    pending_records: list[dict[str, Any]] = []

    with review_lock(lock_path):
        from scripts.index_manifest import index_manifest_is_current, load_index_manifest

        scan = scan_atom_scopes(atoms_dir=eff_atoms, brain_dir=brain_root)
        chroma_dir = os.path.join(brain_root, ".chroma")
        dense_manifest = load_index_manifest(chroma_dir)
        dense_was_current = bool(
            isinstance(dense_manifest, dict)
            and dense_manifest.get("distance_metric") == "cosine"
            and bool(dense_manifest.get("embedding_model"))
            and isinstance(dense_manifest.get("embedding_dimensions"), int)
            and bool(dense_manifest.get("generation_id"))
            and index_manifest_is_current(dense_manifest, eff_atoms)
        )
        os.makedirs(history_dir, exist_ok=True)

        try:
            for fname in list(scan["unscoped"]):  # type: ignore[arg-type]
                fpath = safe_file_in_dir(eff_atoms, str(fname), allowed_exts={".md"}, must_exist=True)
                if not fpath or _is_reparse_or_link(fpath):
                    continue
                orig_bytes = Path(fpath).read_bytes()
                orig_sha = content_sha256(orig_bytes)
                orig_text = orig_bytes.decode("utf-8")
                parsed = split_frontmatter(orig_text)
                if parsed is None:
                    continue
                header_lines, body_lines = parsed
                was_approved = approved_revision_is_current(fpath, brain_dir=brain_root, atoms_dir=eff_atoms)

                updated_fm = set_string_list_field(header_lines, "contexts", [norm_scope], "sources")
                updated_text = render_memory_document(updated_fm, body_lines)

                anchor_dict: dict[str, Any] | None = None
                if was_approved:
                    try:
                        anchor_dict = find_evidence_anchor(updated_text, brain_root)
                    except (EvidenceValidationError, OSError, ValueError):
                        skipped_unverified.append(str(fname))
                        continue

                updated_bytes = updated_text.encode("utf-8")
                after_sha = content_sha256(updated_bytes)
                if after_sha == orig_sha:
                    continue

                bak_path = os.path.join(history_dir, f"{fname}.{orig_sha}.bak")
                if not os.path.exists(bak_path):
                    atomic_write_bytes(bak_path, orig_bytes)
                prior_bytes_by_path[fpath] = orig_bytes
                atomic_write_bytes(fpath, updated_bytes)
                updated_files.append(str(fname))

                rec: dict[str, Any] = {
                    "schema_version": 1,
                    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds"),
                    "action": "approve" if was_approved else "edit",
                    "sub_action": "owner_scope_migration",
                    "actor": clean_actor,
                    "atom_filename": str(fname),
                    "added_contexts": [norm_scope],
                    "content_sha256_before": orig_sha,
                    "content_sha256_after": after_sha,
                    "backup_path": bak_path,
                }
                if anchor_dict:
                    rec.update(anchor_dict)
                pending_records.append(rec)

            if updated_files:
                lex_res = refresh_lexical_index(eff_atoms, brain_root, changed_filenames=updated_files)
                dense_state = "not_active"
                dense_gen: str | None = None
                dense_err: str | None = None
                if dense_was_current and isinstance(dense_manifest, dict):
                    try:
                        from scripts.rebuild_vectors import upsert_changed_vectors

                        d_res = upsert_changed_vectors(
                            atoms_dir=eff_atoms,
                            store_path=chroma_dir,
                            collection_name="atoms",
                            model=str(dense_manifest.get("embedding_model")),
                            changed_filenames=updated_files,
                            pre_verified_current=True,
                            _writer_lock_held=True,
                        )
                        dense_state = "current" if d_res.get("committed") else "stale"
                        dense_gen = (
                            str(d_res.get("generation_id"))
                            if dense_state == "current" and d_res.get("generation_id")
                            else None
                        )
                    except Exception as exc:
                        dense_state = "stale"
                        dense_err = str(exc)[:1000]
                for r_entry in pending_records:
                    r_entry["lexical_index_path"] = lex_res["index_path"]
                    r_entry["dense_index_state"] = dense_state
                    if dense_gen:
                        r_entry["dense_index_generation"] = dense_gen
                    if dense_err:
                        r_entry["dense_index_error"] = dense_err
                append_decision_records(pending_records, decision_log_path)
        except Exception:
            for written_path, orig_raw in prior_bytes_by_path.items():
                try:
                    atomic_write_bytes(written_path, orig_raw)
                except Exception:
                    pass
            if prior_bytes_by_path:
                try:
                    refresh_lexical_index(eff_atoms, brain_root)
                except Exception:
                    pass
            raise

    ledger_info: dict[str, Any] | None = None
    if updated_files:
        ledger_info = commit_ledger_checkpoint(
            brain_dir=brain_root,
            atoms_dir=eff_atoms,
            actor=clean_actor,
            note=f"Owner scope migration: applied {norm_scope} to {len(updated_files)} atom(s).",
        )

    return {
        "status": "completed",
        "actor": clean_actor,
        "scope": norm_scope,
        "modified_count": len(updated_files),
        "modified_atoms": updated_files,
        "skipped_unverified": skipped_unverified,
        "ledger_checkpoint": ledger_info,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit and migrate canonical atom project scopes.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Scan and display scope status without writing (default).")
    parser.add_argument("--apply", action="store_true", help="Apply project:second_brain scope to unscoped atoms.")
    parser.add_argument("--actor", help="Human owner identity authorizing the migration (required with --apply).")
    parser.add_argument("--scope", default="project:second_brain", help="Project scope tag to apply (default: project:second_brain).")
    parser.add_argument(
        "--confirm-migration",
        action="store_true",
        help="Confirm scope migration in non-TTY automation once authorized by a human owner.",
    )
    parser.add_argument("--atoms-dir", default=None, help="Optional override path to 2-atoms directory.")
    parser.add_argument("--brain-dir", default=None, help="Optional override path to vault root.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scan = scan_atom_scopes(atoms_dir=args.atoms_dir, brain_dir=args.brain_dir)

    print("=== ATOM PROJECT SCOPE AUDIT ===")
    print(f"Total atoms: {scan['total_atoms']}")
    print(f"Scoped atoms ({scan['scoped_count']}):")
    for s in scan["scoped"]:  # type: ignore[attr-defined]
        print(f"  - {s['filename']}: {s['scopes']}")
    print(f"Unscoped atoms ({scan['unscoped_count']}): {scan['unscoped_count']} atoms currently have contexts: []")

    if not args.apply:
        print("\n[*] Dry-run complete. No files were modified.")
        print("[!] Note: Approved atoms with pinned hashes in retrieval-evaluation-proposed.json (v3.0.0) require benchmark re-anchoring after scope migration.")
        return 0

    if not args.actor:
        print("\n[!] Error: --actor <owner> is strictly required when applying scope modifications.", file=sys.stderr)
        return 1

    try:
        res = apply_scope_migration(
            scope=args.scope,
            actor=args.actor,
            brain_dir=args.brain_dir,
            atoms_dir=args.atoms_dir,
            interactive_tty_verified=(True if args.confirm_migration else None),
        )
    except (PermissionError, ValueError) as exc:
        print(f"\n[!] Blocked: {exc}", file=sys.stderr)
        return 1

    print(f"[+] Successfully migrated {res['modified_count']} atoms with scope {res['scope']} as '{res['actor']}'.")
    if res["skipped_unverified"]:
        print(f"[!] Skipped {len(res['skipped_unverified'])} approved atom(s) whose source anchors could not be re-verified.")
    if res["modified_count"]:
        print("[!] Reminder: Re-pin any modified atom SHA-256 hashes referenced in docs/retrieval-evaluation-proposed.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
