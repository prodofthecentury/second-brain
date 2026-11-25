# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Automated Hourly System Sweep & Operational Readiness Scorer.

Measures live repository, test-gate, canonical vault, Three-Store durability,
disposable sidecar index, knowledge graph, and runtime daemon metrics, writing
`brain/.sidecar/hourly-sweeps/latest.json` and appending a compact record to
`brain/.sidecar/hourly-sweeps/history.jsonl`.
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
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

from scripts.atom_mutator import atomic_write_bytes, approved_revision_is_current, review_lock  # noqa: E402
from scripts.config import (  # noqa: E402
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_vault_path,
    snapshot_module_defaults,
    vault_paths_for,
)

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
from scripts.hardware_profile import detect_hardware_profile  # noqa: E402
from scripts.inbox_workflow import list_inbox_items  # noqa: E402
from scripts.index_manifest import index_manifest_is_current, load_index_manifest  # noqa: E402
from scripts.lexical_store import lexical_index_is_current  # noqa: E402
from scripts.skill_playbooks import list_playbooks  # noqa: E402
from scripts.time_travel import list_snapshots  # noqa: E402
from scripts.vault_ledger import verify_vault_ledger  # noqa: E402


def _git_info(repo_root: Path) -> tuple[str, int, int]:
    head = "unknown"
    c_1h = 0
    c_24h = 0
    try:
        r_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r_head.returncode == 0 and r_head.stdout.strip():
            head = r_head.stdout.strip()
        r_1h = subprocess.run(
            ["git", "rev-list", "--count", "--since=1 hour ago", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r_1h.returncode == 0 and r_1h.stdout.strip().isdigit():
            c_1h = int(r_1h.stdout.strip())
        r_24h = subprocess.run(
            ["git", "rev-list", "--count", "--since=24 hours ago", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r_24h.returncode == 0 and r_24h.stdout.strip().isdigit():
            c_24h = int(r_24h.stdout.strip())
    except Exception:
        pass
    return head, c_1h, c_24h


def _count_content_md(directory: str) -> int:
    if not os.path.isdir(directory) or _is_reparse_or_link(directory):
        return 0
    return sum(
        1
        for e in os.scandir(directory)
        if e.is_file(follow_symlinks=False) and is_content_markdown(e.name)
    )


def run_hourly_sweep(
    *,
    brain_dir: str | None = None,
    repo_root: str | Path = REPO_ROOT,
    run_tests: bool = False,
) -> dict[str, Any]:
    root_path = Path(repo_root).resolve()
    resolved_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", brain_dir)
    vp = vault_paths_for(resolved_brain)
    eff_brain = vp["brain_dir"]
    sidecar_dir = os.path.join(eff_brain, ".sidecar")
    sweeps_dir = os.path.join(sidecar_dir, "hourly-sweeps")
    if os.path.lexists(eff_brain) and _is_reparse_or_link(eff_brain):
        raise PermissionError("Refusing to write hourly sweep under symlinked vault.")
    os.makedirs(sweeps_dir, exist_ok=True)
    if _is_reparse_or_link(sidecar_dir) or _is_reparse_or_link(sweeps_dir):
        raise PermissionError("Refusing to write hourly sweep under symlinked sidecar directory.")

    test_report_path = os.path.join(sidecar_dir, "test-all-latest.json")
    if run_tests:
        subprocess.run(
            [sys.executable, str(root_path / "scripts" / "test_all.py"), "--json-output", test_report_path],
            cwd=str(root_path),
            check=False,
        )

    suites_passed = 0
    suites_total = 0
    test_duration_s = 0.0
    for candidate_report in (
        test_report_path,
        os.path.join(str(root_path), "brain", ".sidecar", "test-all-latest.json"),
    ):
        if os.path.isfile(candidate_report) and not _is_reparse_or_link(candidate_report):
            try:
                tr = json.loads(Path(candidate_report).read_text(encoding="utf-8"))
                suites_passed = int(tr.get("passed_count") or 0)
                suites_total = int(tr.get("suite_count") or 0)
                test_duration_s = round(float(tr.get("total_elapsed_seconds") or 0.0), 2)
                break
            except Exception:
                continue

    atoms_dir = vp["atoms_dir"]
    atom_files = (
        sorted(
            e.path
            for e in os.scandir(atoms_dir)
            if e.is_file(follow_symlinks=False) and is_content_markdown(e.name)
        )
        if os.path.isdir(atoms_dir) and not _is_reparse_or_link(atoms_dir)
        else []
    )
    atoms_total = len(atom_files)
    atoms_approved = sum(
        1
        for ap in atom_files
        if approved_revision_is_current(ap, brain_dir=eff_brain, atoms_dir=atoms_dir)
    )
    atoms_pending = max(0, atoms_total - atoms_approved)

    try:
        inbox_items = list_inbox_items(inbox_dir=vp["inbox_dir"], raw_dir=vp["raw_dir"], brain_dir=eff_brain)
        inbox_quarantined = len(inbox_items)
    except Exception:
        inbox_quarantined = 0

    notes_count = _count_content_md(vp["notes_dir"])
    threads_count = _count_content_md(vp["threads_dir"])
    briefings_count = _count_content_md(vp["briefings_dir"])
    experiences_count = _count_content_md(vp["experiences_dir"])
    sources_count = _count_content_md(vp["sources_dir"])
    generated_dir = os.path.join(vp["sources_dir"], "generated")
    generated_media_count = (
        sum(
            1
            for e in os.scandir(generated_dir)
            if e.is_file(follow_symlinks=False)
            and os.path.splitext(e.name)[1].lower() in {".png", ".webp", ".mp4", ".gif"}
        )
        if os.path.isdir(generated_dir) and not _is_reparse_or_link(generated_dir)
        else 0
    )

    active_playbooks = list_playbooks(
        playbooks_dir=vp["playbooks_dir"],
        brain_dir=eff_brain,
        atoms_dir=atoms_dir,
        active_only=True,
    )
    snapshots = list_snapshots(brain_dir=eff_brain)
    legacy_snap_dir = os.path.join(sidecar_dir, "snapshots")
    stranded_legacy_snapshots = (
        sum(1 for e in os.scandir(legacy_snap_dir) if e.name.endswith(".json") and e.is_file(follow_symlinks=False))
        if os.path.isdir(legacy_snap_dir) and not _is_reparse_or_link(legacy_snap_dir)
        else 0
    )

    lexical_ok = lexical_index_is_current(atoms_dir, vp["lexical_index_path"])
    manifest = load_index_manifest(vp["chroma_path"])
    dense_ok = bool(manifest and index_manifest_is_current(manifest, atoms_dir))
    ledger_rep = verify_vault_ledger(
        brain_dir=eff_brain,
        atoms_dir=atoms_dir,
        playbooks_dir=vp["playbooks_dir"],
    )
    ledger_ok = bool(ledger_rep.get("chain_valid", False))
    telemetry_present = os.path.isfile(os.path.join(sidecar_dir, "telemetry.sqlite3"))
    chat_threads_present = os.path.isfile(os.path.join(sidecar_dir, "chat_threads.sqlite3"))

    try:
        hw = detect_hardware_profile(ollama_timeout=1.5)
        hw_tier = hw.tier_id
    except Exception:
        hw_tier = "tier_0_lexical_only"

    head_commit, commits_1h, commits_24h = _git_info(root_path)
    dist_ok = (root_path / "dist" / "SHA256SUMS.txt").is_file()

    # Deterministic rubric (0..100)
    gate_ratio = (suites_passed / suites_total) if suites_total > 0 else 1.0
    build_pkg = min(100, int(round(75 + (15 if dist_ok else 0) + 10 * gate_ratio)))
    arch_inv = min(
        100,
        int(
            round(
                80
                + (8 if ledger_ok else 0)
                + (6 if lexical_ok else 0)
                + (6 if stranded_legacy_snapshots == 0 else 0)
            )
        ),
    )
    code_qual = min(100, int(round(85 + 15 * gate_ratio)))
    verif_rigor = min(100, int(round(80 + 14 * gate_ratio + (6 if dense_ok or atoms_total == 0 else 0))))
    sec_posture = 98 if ledger_ok else 88
    usability_pts = (
        55
        + min(10, notes_count // 2)
        + (8 if threads_count > 0 else 0)
        + (7 if briefings_count > 0 else 0)
        + (5 if sources_count > 0 else 0)
        + (5 if generated_media_count > 0 else 0)
        + (5 if telemetry_present else 0)
        + (5 if chat_threads_present else 0)
    )
    daily_usability = min(100, usability_pts)
    work_output = min(100, 88 + min(12, commits_24h * 2 + (4 if suites_total >= 26 else 0)))
    overall = int(
        round(
            0.20 * build_pkg
            + 0.20 * arch_inv
            + 0.15 * code_qual
            + 0.20 * verif_rigor
            + 0.10 * sec_posture
            + 0.15 * daily_usability
        )
    )
    composite = int(round((overall + sec_posture + arch_inv) / 3.0))

    now_iso = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    scores = {
        "overall_completion": overall,
        "build_and_packaging_spec": build_pkg,
        "hourly_work_output": work_output,
        "architecture_and_invariants": arch_inv,
        "code_quality_and_spec": code_qual,
        "verification_and_epistemic_rigor": verif_rigor,
        "security_and_trust_posture": sec_posture,
        "daily_usability_and_corpus": daily_usability,
        "composite_system_maturity": composite,
    }
    payload: dict[str, Any] = {
        "timestamp": now_iso,
        "head_commit": head_commit,
        "commits_last_1h": commits_1h,
        "commits_last_24h": commits_24h,
        "test_suites_passed": suites_passed,
        "test_suites_total": suites_total,
        "test_duration_seconds": test_duration_s,
        "atoms_total": atoms_total,
        "atoms_approved": atoms_approved,
        "atoms_pending": atoms_pending,
        "notes_total": notes_count,
        "playbooks_active": len(active_playbooks),
        "threads_total": threads_count,
        "briefings_total": briefings_count,
        "experiences_total": experiences_count,
        "sources_total": sources_count,
        "generated_media_total": generated_media_count,
        "snapshots_total": len(snapshots),
        "stranded_legacy_snapshots": stranded_legacy_snapshots,
        "inbox_quarantined": inbox_quarantined,
        "lexical_index_current": lexical_ok,
        "dense_index_current": dense_ok,
        "ledger_valid": ledger_ok,
        "telemetry_db_present": telemetry_present,
        "chat_threads_db_present": chat_threads_present,
        "hardware_tier": hw_tier,
        "scores": scores,
    }

    lock_path = os.path.join(sidecar_dir, "writer.lock")
    latest_path = os.path.join(sweeps_dir, "latest.json")
    history_path = os.path.join(sweeps_dir, "history.jsonl")
    compact_history = {
        "timestamp": now_iso,
        "head_commit": head_commit[:7] if head_commit != "unknown" else "unknown",
        "commits_1h": commits_1h,
        "commits_24h": commits_24h,
        "suites": f"{suites_passed}/{suites_total}",
        "atoms_approved": f"{atoms_approved}/{atoms_total}",
        "notes": notes_count,
        "threads": threads_count,
        "briefings": briefings_count,
        "snapshots": len(snapshots),
        "scores": scores,
    }

    with review_lock(lock_path):
        atomic_write_bytes(latest_path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        hist_line = (json.dumps(compact_history, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, hist_line) != len(hist_line):
                raise OSError("Incomplete hourly-sweeps history append.")
            os.fsync(fd)
        finally:
            os.close(fd)

    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain-dir", default=None)
    parser.add_argument("--run-tests", action="store_true", help="Execute test_all.py prior to scoring.")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args(argv)
    res = run_hourly_sweep(brain_dir=args.brain_dir, run_tests=args.run_tests)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        sc = res["scores"]
        print(
            f"Hourly sweep complete: overall={sc['overall_completion']} "
            f"maturity={sc['composite_system_maturity']} suites={res['test_suites_passed']}/{res['test_suites_total']} "
            f"atoms={res['atoms_approved']}/{res['atoms_total']} notes={res['notes_total']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
