# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Review captured articles in ``sources/`` and make approved ones searchable.

Scout writes article Markdown under ``sources/`` and a quarantined ``0-raw/*.intake``
pointer. Nothing in that pair is in the atom index, so recall cannot see it, and
there was no bulk path that both records an owner decision and indexes the text.

Approving a source does not create a citable memory atom. It records an owner
attestation, marks a matching intake pointer approved so a later refinery run may
draft atoms, and indexes the current bytes in a disposable sidecar. Search hits
are labeled ``approved_source_capture``. Citation still requires a separate atom
approval. Editing the file drops it out of search until it is approved again.
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
import json
import os
from pathlib import Path
import sys


from scripts.config import file_sha256, resolve_active_brain_dir
from scripts.inbox_workflow import _rewrite_intake_pointer, scan_inbox_payload_risks
from scripts.lexical_store import rebuild_lexical_index, search_lexical_index
from scripts.owner_attestation import (
    OwnerAttestationError,
    require_attestation,
    split_grant_args,
)
from scripts.refinery_targets import intake_status
from scripts.security_fs import restrict_security_file_permissions

MAX_SOURCE_BYTES = 8 * 1024 * 1024
LEDGER_NAME = "source-approvals.jsonl"
MIRROR_DIRNAME = "approved-sources-mirror"
INDEX_NAME = "approved-sources.sqlite3"
MANIFEST_NAME = "approved-sources-manifest.json"


class SourceReviewError(ValueError):
    """A source review request could not be applied."""


def _brain(brain_dir: str | Path | None) -> Path:
    return Path(os.path.abspath(str(brain_dir or resolve_active_brain_dir())))


def _sources_dir(brain: Path) -> Path:
    return brain / "sources"


def _ledger_path(brain: Path) -> Path:
    return brain / "0-raw" / "archive" / LEDGER_NAME


def _sidecar(brain: Path) -> Path:
    return brain / ".sidecar"


def _load_ledger(brain: Path) -> list[dict]:
    path = _ledger_path(brain)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _append_ledger(brain: Path, record: dict) -> None:
    path = _ledger_path(brain)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("Incomplete write to source approval ledger.")
        os.fsync(fd)
    finally:
        os.close(fd)
    restrict_security_file_permissions(path)


def _safe_source_path(brain: Path, filename: str) -> Path:
    name = str(filename or "").strip()
    if not name or name != Path(name).name or not name.lower().endswith(".md") or name.startswith("."):
        raise SourceReviewError("Source filename must be one Markdown file directly under sources/.")
    path = _sources_dir(brain) / name
    if not path.is_file() or path.is_symlink():
        raise SourceReviewError(f"Source file not found: {name}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise SourceReviewError(f"Source file exceeds {MAX_SOURCE_BYTES} bytes: {name}")
    return path


def _latest_decision(brain: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in _load_ledger(brain):
        name = str(row.get("filename") or "")
        if name:
            latest[name] = row
    return latest


def _approved_now(brain: Path) -> dict[str, dict]:
    """Filename -> ledger row, only when the file bytes still match the approval."""
    approved: dict[str, dict] = {}
    for name, row in _latest_decision(brain).items():
        if row.get("action") != "approve":
            continue
        try:
            path = _safe_source_path(brain, name)
        except SourceReviewError:
            continue
        digest = file_sha256(str(path))
        if digest and digest == str(row.get("sha256") or ""):
            approved[name] = row
    return approved


def list_source_queue(brain_dir: str | Path | None = None) -> list[dict]:
    """List top-level ``sources/*.md`` files and whether the current bytes are approved."""
    brain = _brain(brain_dir)
    root = _sources_dir(brain)
    if not root.is_dir():
        return []
    approved = _approved_now(brain)
    items: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or entry.is_symlink() or not entry.name.lower().endswith(".md"):
            continue
        if entry.name == "README.md" or entry.name.startswith("."):
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        digest = file_sha256(str(entry)) or ""
        risks = list(scan_inbox_payload_risks(text))
        row = approved.get(entry.name)
        items.append(
            {
                "filename": entry.name,
                "rel_path": f"sources/{entry.name}",
                "sha256": digest,
                "size_bytes": entry.stat().st_size,
                "approved": row is not None,
                "risk_flags": risks,
                "searchable": row is not None,
                "epistemic_label": "approved_source_capture" if row else "unapproved_source",
            }
        )
    return items


def _matching_intakes(brain: Path, filename: str) -> list[Path]:
    raw = brain / "0-raw"
    if not raw.is_dir():
        return []
    needle = f"sources/{filename}"
    found: list[Path] = []
    for entry in sorted(raw.glob("*.intake")):
        if not entry.is_file() or entry.is_symlink():
            continue
        text = entry.read_text(encoding="utf-8", errors="replace")
        if needle in text and intake_status(text) == "quarantined":
            found.append(entry)
    return found


def _sync_search_index(brain: Path) -> dict[str, str]:
    approved = _approved_now(brain)
    sidecar = _sidecar(brain)
    mirror = sidecar / MIRROR_DIRNAME
    index_path = sidecar / INDEX_NAME
    manifest_path = sidecar / MANIFEST_NAME
    desired = {name: str(row["sha256"]) for name, row in approved.items()}
    current: dict | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            current = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            current = None
    if current == desired and index_path.is_file():
        return desired
    sidecar.mkdir(parents=True, exist_ok=True)
    if mirror.exists():
        for child in mirror.iterdir():
            if child.is_file() and not child.is_symlink():
                child.unlink()
    else:
        mirror.mkdir(parents=True, exist_ok=True)
    for name in sorted(desired):
        src = _safe_source_path(brain, name)
        (mirror / name).write_bytes(src.read_bytes())
    rebuild_lexical_index(str(mirror), str(index_path))
    manifest_path.write_text(json.dumps(desired, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return desired


def approve_sources(
    items: list[dict],
    *,
    actor: str,
    attestation: dict,
    acknowledge_risks: bool = False,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> list[dict]:
    """CAS-approve one or more source files and refresh the searchable sidecar."""
    brain = _brain(brain_dir)
    try:
        recorded = require_attestation(attestation, actor=actor, state_dir=state_dir)
    except OwnerAttestationError as exc:
        raise SourceReviewError(str(exc)) from exc
    if not items:
        raise SourceReviewError("No source files were selected.")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    decisions: list[dict] = []
    for item in items:
        filename = str(item.get("filename") or "")
        expected = str(item.get("expected_sha256") or item.get("sha256") or "").strip().lower()
        path = _safe_source_path(brain, filename)
        actual = file_sha256(str(path)) or ""
        if len(expected) != 64 or actual != expected:
            raise SourceReviewError(
                f"CAS mismatch for {filename}: expected {expected or '(missing)'}, found {actual}."
            )
        text = path.read_text(encoding="utf-8")
        risks = list(scan_inbox_payload_risks(text))
        if risks and not acknowledge_risks:
            raise SourceReviewError(
                f"{filename} has risk flags {risks}. Inspect it and pass acknowledge_risks to approve."
            )
        intakes = _matching_intakes(brain, filename)
        updated_intakes: list[str] = []
        for pointer in intakes:
            rewritten = _rewrite_intake_pointer(
                pointer.read_text(encoding="utf-8"),
                new_status="approved",
                actor=recorded["actor"],
                timestamp=now,
            )
            pointer.write_text(rewritten, encoding="utf-8")
            updated_intakes.append(pointer.name)
        decision = {
            "schema_version": 1,
            "action": "approve",
            "recorded_at": now,
            "filename": filename,
            "rel_path": f"sources/{filename}",
            "sha256": actual,
            "actor": recorded["actor"],
            "attestation": recorded,
            "risk_flags": risks,
            "acknowledged_risks": bool(acknowledge_risks and risks),
            "intake_updated": updated_intakes,
            "epistemic_label": "approved_source_capture",
            "citable_claim": False,
        }
        _append_ledger(brain, decision)
        decisions.append(decision)
    _sync_search_index(brain)
    return decisions


def approve_clean_sources(
    *,
    actor: str,
    attestation: dict,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> dict:
    """Approve every unapproved source that has no DLP risk flags. Risky files stay queued."""
    queue = list_source_queue(brain_dir)
    clean = [item for item in queue if not item["approved"] and not item["risk_flags"]]
    skipped = [item["filename"] for item in queue if not item["approved"] and item["risk_flags"]]
    if not clean:
        return {"approved": [], "skipped_risky": skipped, "note": "No clean unapproved sources."}
    decisions = approve_sources(
        [{"filename": item["filename"], "expected_sha256": item["sha256"]} for item in clean],
        actor=actor,
        attestation=attestation,
        brain_dir=brain_dir,
        state_dir=state_dir,
    )
    return {"approved": decisions, "skipped_risky": skipped}


def search_approved_sources(
    query: str,
    *,
    limit: int = 8,
    brain_dir: str | Path | None = None,
) -> list[dict]:
    """Search only source files whose current bytes have an owner approval."""
    text = str(query or "").strip()
    if not text:
        raise SourceReviewError("A search query is required.")
    brain = _brain(brain_dir)
    approved = _sync_search_index(brain)
    if not approved:
        return []
    mirror = _sidecar(brain) / MIRROR_DIRNAME
    index_path = _sidecar(brain) / INDEX_NAME
    hits = search_lexical_index(text, str(index_path), n_results=max(1, int(limit)), atoms_dir=str(mirror))
    results: list[dict] = []
    for hit in hits:
        if hit.filename not in approved or hit.content_sha256 != approved[hit.filename]:
            continue
        results.append(
            {
                "filename": hit.filename,
                "rel_path": f"sources/{hit.filename}",
                "sha256": hit.content_sha256,
                "rank": hit.rank,
                "snippet": hit.content[:480],
                "epistemic_label": "approved_source_capture",
                "citable_claim": False,
                "note": "Owner-approved source text. Not a distilled memory atom; do not cite it as a verified claim.",
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    cleaned, grant = split_grant_args(argv)
    parser = argparse.ArgumentParser(
        prog="second-brain sources",
        description="Review captured sources and search the ones you have approved.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    queue = sub.add_parser("queue", help="List sources and whether the current bytes are searchable.")
    queue.add_argument("--json", action="store_true")
    approve = sub.add_parser("approve", help="Approve one source file after checking its SHA-256.")
    approve.add_argument("filename")
    approve.add_argument("--expected-sha256", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--acknowledge-risks", action="store_true")
    clean = sub.add_parser("approve-clean", help="Approve every unapproved source that has no risk flags.")
    clean.add_argument("--actor", required=True)
    search = sub.add_parser("search", help="Search owner-approved source captures.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--json", action="store_true")
    args = parser.parse_args(cleaned)
    try:
        if args.command == "queue":
            items = list_source_queue()
            if args.json:
                print(json.dumps(items, indent=2))
            elif not items:
                print("No captured sources.")
            else:
                for item in items:
                    state = "approved" if item["approved"] else "pending"
                    risks = f" risks={','.join(item['risk_flags'])}" if item["risk_flags"] else ""
                    print(f"{state}\t{item['filename']}\t{item['sha256'][:12]}{risks}")
            return 0
        if args.command == "search":
            hits = search_approved_sources(args.query, limit=args.limit)
            print(json.dumps(hits, indent=2))
            return 0
        from scripts.owner_attestation import attestation_from_grant

        attestation = attestation_from_grant(grant)
        if args.command == "approve":
            decisions = approve_sources(
                [{"filename": args.filename, "expected_sha256": args.expected_sha256}],
                actor=args.actor,
                attestation=attestation,
                acknowledge_risks=bool(args.acknowledge_risks),
            )
        else:
            decisions = approve_clean_sources(actor=args.actor, attestation=attestation)
        print(json.dumps(decisions, indent=2))
        return 0
    except (SourceReviewError, OwnerAttestationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
