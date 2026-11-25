# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Unified CI-style end-to-end test orchestrator for Second Brain.

Executes environment diagnostics, curated dataset revision-binding validation,
cryptographic source anchor auditing, unit/security/penetration regression tests,
Goal 2 document/MCP/DLP/NLI tests, Goal 3 Desktop/VS Code/Dashboard/Wheel suites,
real ChromaDB staged-generation canary, and live end-to-end smoke verification.

Run:
    uv run python scripts/test_all.py
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


SUITES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "doctor",
        "Environment, runtime ABI, vault directories & index health (scripts/doctor.py)",
        (str(SCRIPTS_DIR / "doctor.py"),),
    ),
    (
        "evaluation_dataset",
        "Curated 90-case dataset schema, splits & SHA-256 revision binding (scripts/evaluation_dataset.py)",
        (str(SCRIPTS_DIR / "evaluation_dataset.py"),),
    ),
    (
        "audit_sources",
        "Cryptographic source byte-offset & SHA-256 anchor audit (scripts/audit_sources.py)",
        (str(SCRIPTS_DIR / "audit_sources.py"), "--check"),
    ),
    (
        "system_safety_tests",
        "Unit, CAS, grounding, RRF/project scoping & live security penetration suite (scripts/system_safety_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "system_safety_tests.py")),
    ),
    (
        "phase2_safety_tests",
        "Phase 2 hardware tiers, inbox promotion, online consent/redaction, cognitive core & loopback API suite (scripts/phase2_safety_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase2_safety_tests.py")),
    ),
    (
        "phase3_cognitive_tests",
        "Phase 3A context/fitness ranking, owner-gated Playbooks & grounded Executive Planner suite (scripts/phase3_cognitive_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase3_cognitive_tests.py")),
    ),
    (
        "phase4_product_tests",
        "Phase 4 scale engine, zero-friction notes, one-click verify, streaming chat, product API, import & IDE connector suite (scripts/phase4_product_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase4_product_tests.py")),
    ),
    (
        "phase5_god_features_tests",
        "Desktop installers/tray, comparative SOTA benchmark, Merkle vault ledger, contradiction radar & time-travel snapshots (scripts/phase5_god_features_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase5_god_features_tests.py")),
    ),
    (
        "goal2_completion_tests",
        "Goal 2 document ingestion, chunked refinery/incremental vectors, research view, multi-host streaming DLP, NLI radar, 17-tool MCP, 6-host connectors & SOTA benchmark invariants (scripts/goal2_completion_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "goal2_completion_tests.py")),
    ),
    (
        "goal3_backend_contract_tests",
        "Goal 3 backend API parity, document extractors, port discovery, research view & Merkle leaf contracts (scripts/goal3_backend_contract_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "goal3_backend_contract_tests.py")),
    ),
    (
        "goal4_sync_and_ui_tests",
        "Goal 4 Zero-Knowledge E2EE multi-device vault sync, crypto tamper/rollback/AAD checks, API/SDK owner gating, UI parity & CI/release hygiene (scripts/goal4_sync_and_ui_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "goal4_sync_and_ui_tests.py")),
    ),
    (
        "goal5_clipper_and_workspaces_tests",
        "Goal 5 local-first Browser Web Clipper MV3/CORS/DLP/snapshot provenance & Multi-Vault Workspaces isolation, hot-switch, per-vault tokens & opt-in federated recall (scripts/goal5_clipper_and_workspaces_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "goal5_clipper_and_workspaces_tests.py")),
    ),
    (
        "v040_sota_upgrades_tests",
        "v0.4.0 SOTA upgrades: auto-wired qwen2.5-coder:7b, >64-atom O(1) Chroma upserts, ONNX/interaction cross-encoder, multi-turn chat threads, 0-FP NLI & auto-linking graph, Readability/1-click clipper, layout/OCR PDF, S3 SigV4, DPAPI/Keychain, Auto-Sync & 3-way conflict resolver (scripts/v040_sota_upgrades_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "v040_sota_upgrades_tests.py")),
    ),
    (
        "v050_sota_complete_tests",
        "v0.5.0 complete SOTA suite: SHA-256 ONNX downloader & [reranker] extra, clause-aligned DeBERTa NLI, qwen3 GGUF RAM-fit & <think> stripping, bi-temporal valid_from/valid_until/as_of, and SBID1 E2EE Mobile Inbox Drop PWA + S3 SigV4 (scripts/v050_sota_complete_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "v050_sota_complete_tests.py")),
    ),
    (
        "v100_final_completion_tests",
        "v1.0.0 production completion suite: multimodal /v1/ingest/media & DLP quarantine, Git post-commit distiller, bi-temporal Cross-Encoder snapshot recall, daemon vector/auto-sync, portable .sbvault backup/restore, bidirectional SBID1/SBMR1 mobile recall & WinGet/Homebrew/PWA manifests (scripts/v100_final_completion_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "v100_final_completion_tests.py")),
    ),
    (
        "creative_media_tests",
        "Creative media provenance, Pillow sanitization, CAS asset binding & security boundary suite (scripts/creative_media_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "creative_media_tests.py")),
    ),
    (
        "stage4_executable_skills_tests",
        "Stage 4 owner-promoted Executable Skills, closed-action sandbox, kill-switch, rollback & hash-chained audit suite (scripts/stage4_executable_skills_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "stage4_executable_skills_tests.py")),
    ),
    (
        "mobile_vault_client_tests",
        "Cross-runtime Python <-> Node WebCrypto on-device Mobile Vault Client E2EE sync, epistemic recall & offline drop suite (scripts/mobile_vault_client_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "mobile_vault_client_tests.py")),
    ),
    (
        "bugfix_1a_regression_tests",
        "1A live bugs, Stage 4 sandbox hardening, keychain sealing, Merkle/snapshot parity, skill-executions sync merge, ephemeral clipper port, numeric 422 & minimal-wheel imports (scripts/bugfix_1a_regression_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "bugfix_1a_regression_tests.py")),
    ),
    (
        "desktop_ui_tests",
        "Electron Desktop DOM/XSS/Markdown/graph/pagination behavioral suite & packaged smoke-test (scripts/run_desktop_tests.py)",
        (str(SCRIPTS_DIR / "run_desktop_tests.py"),),
    ),
    (
        "vscode_extension_tests",
        "VS Code extension SSE cancellation, singleton chat, CAS notes & governance suite (scripts/run_vscode_tests.py)",
        (str(SCRIPTS_DIR / "run_vscode_tests.py"),),
    ),
    (
        "dashboard_apptest_tests",
        "Streamlit AppTest dashboard auth, chat modes, governance, snapshots & import suite (scripts/dashboard_apptest_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "dashboard_apptest_tests.py")),
    ),
    (
        "parity_1c_tests",
        "1C cross-layer parity: bi-temporal graph API/SDK, 27-tool MCP + ONNX NLI, CLI commands & PWA/clipper manifests/icons (scripts/parity_1c_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "parity_1c_tests.py")),
    ),
    (
        "wheel_packaging_tests",
        "Clean wheel namespace, isolated temp-venv CLI/API/dashboard, VSIX, icons & offline wheelhouse suite (scripts/wheel_packaging_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "wheel_packaging_tests.py")),
    ),
    (
        "phase6_release_and_stores_tests",
        "Epic 2A multi-store publishers (CWS v2, AMO v5, Edge, PyPI, VSIX, WinGet PR), Azure/DMG notarization, Ed25519 manifest sigs & Fastlane workflows (scripts/phase6_release_and_stores_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase6_release_and_stores_tests.py")),
    ),
    (
        "phase7_platform_and_connectors_tests",
        "Epic 2B video ingestion, desktop auto-updater, bidirectional mobile E2EE, 14 IDE/AI host connectors, Safari/media browser clipper & optional live benchmarks (scripts/phase7_platform_and_connectors_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase7_platform_and_connectors_tests.py")),
    ),
    (
        "phase8_ecosystem_and_canvas_tests",
        "Epic 2C dual-mode Build Canvas, speculative llama.cpp decoding, Kokoro ONNX TTS voice loop, opt-in Screen OCR/External Indexer/OpenAI Proxy/Tunnel, Jarvis orchestrator, 5-panel critique & quarantined skill packs (scripts/phase8_ecosystem_and_canvas_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase8_ecosystem_and_canvas_tests.py")),
    ),
    (
        "phase9_gen4_cognition_tests",
        "Epic 2D Gen-4 cognitive architecture: Fractal Zoom L0-L5, Predictive L0-L3 Bus invalidation, Principles & Mental Models CAS/Merkle, Knowledge Gravity & cold-archive, Living Concepts timelines & spreading activation, Meta-Router/Swarm/Debate, Nightly Replay, World-Model Simulator, Self-Model/Curiosity & ADR gates (scripts/phase9_gen4_cognition_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase9_gen4_cognition_tests.py")),
    ),
    (
        "phase10_math_and_formal_discovery_tests",
        "Epic 2E quantitative reasoning, neuro-symbolic verification & formal proof search: LaTeX preservation, symbolic equivalence & Minerva majority@k, AST-jailed sandbox & CGV/DUP/ToRA loop, 4D Process Rubric & anti-sycophancy refiner, SafeVerify EVOLVE-BLOCK/Ralph/Agent-D Plackett-Luce Gibbs Elo & Subgoal Cache, perturbation invariance & decontamination (scripts/phase10_math_and_formal_discovery_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "phase10_math_and_formal_discovery_tests.py")),
    ),
    (
        "quantum_leap_api_sdk_tests",
        "Quantum Leap end-to-end loopback HTTP coverage for all Phase 9-10 routes, owner/opt-in security gates, full Phase 8-10 MemoryApiClient SDK parity, meta_router deliberative chat & CLI parity (scripts/quantum_leap_api_sdk_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "quantum_leap_api_sdk_tests.py")),
    ),
    (
        "goal_1b_completion_tests",
        "1B completion regression suite: Capacitor mobile native/JS, Creative Media fail-closed & H.264 MP4, ONNX unconditional rerank, registry CLI & AMO polling, Linux AppImage/deb sigs, fair SOTA benchmarks, macOS/GPU/PDF OCR, dynamic vault paths, dashboard cookie, chat REPL shim & smart critic (scripts/goal_1b_completion_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "goal_1b_completion_tests.py")),
    ),
    (
        "release_and_benchmark_tests",
        "Diffusers local/SDXL pipelines, owner gates (Track 3/4), release publisher, Ed25519 verification & doc/CLI parity suite (scripts/release_and_benchmark_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "release_and_benchmark_tests.py")),
    ),
    (
        "live_runtime_integration_tests",
        "Unmocked ONNX weights, faster-whisper tiny.en, Ollama llava/nomic-embed/qwen2.5-coder:7b, PyAV libx264 & loopback TCP daemon suite (scripts/live_runtime_integration_tests.py)",
        ("-m", "unittest", str(SCRIPTS_DIR / "live_runtime_integration_tests.py")),
    ),
    (
        "scale_benchmark_1500",
        "Corpus-scale correctness canary on a 1,500-note isolated vault (scripts/scale_benchmark.py)",
        (str(SCRIPTS_DIR / "scale_benchmark.py"), "--notes", "1500", "--queries", "20"),
    ),
    (
        "chroma_rebuild_canary",
        "Real ChromaDB staged vector rebuild, cosine activation & handle cleanup canary (scripts/chroma_rebuild_canary.py)",
        (str(SCRIPTS_DIR / "chroma_rebuild_canary.py"),),
    ),
    (
        "archive_manifest_canary",
        "Refinery archive manifest SHA-256 lineage & sticky origin canary (scripts/archive_manifest_canary.py)",
        (str(SCRIPTS_DIR / "archive_manifest_canary.py"),),
    ),
    (
        "smoke_test",
        "End-to-end vault intake, grounding, lexical & dense recall smoke test (scripts/smoke_test.py)",
        (str(SCRIPTS_DIR / "smoke_test.py"),),
    ),
)


def run_suite(suite_id: str, description: str, cmd_args: tuple[str, ...]) -> dict:
    command = [sys.executable, *cmd_args]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed_s = round(time.perf_counter() - started, 3)
    passed = completed.returncode == 0
    return {
        "suite_id": suite_id,
        "description": description,
        "command": command,
        "passed": passed,
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed_s,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop immediately on the first failing test suite.",
    )
    parser.add_argument(
        "--json-output",
        help="Optional path to write full JSON execution report.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        help="Optional subset of suite IDs to run.",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        help="Optional subset of suite IDs to skip (cannot be combined with --only).",
    )
    args = parser.parse_args(argv)

    if args.only and args.exclude:
        print("Cannot combine --only and --exclude.", file=sys.stderr)
        return 2

    all_ids = {s[0] for s in SUITES}
    selected = SUITES
    if args.only:
        wanted = set(args.only)
        selected = tuple(s for s in SUITES if s[0] in wanted)
        unknown = wanted - all_ids
        if unknown:
            print(f"Unknown suite ID(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
    elif args.exclude:
        excluded = set(args.exclude)
        unknown = excluded - all_ids
        if unknown:
            print(f"Unknown excluded suite ID(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        selected = tuple(s for s in SUITES if s[0] not in excluded)

    print("=" * 84)
    print("SECOND BRAIN — UNIFIED PRODUCTION TEST GATE")
    print("=" * 84)

    overall_start = time.perf_counter()
    results: list[dict] = []

    for index, (suite_id, description, cmd_args) in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] Running {suite_id:<23} ... ", end="", flush=True)
        outcome = run_suite(suite_id, description, cmd_args)
        results.append(outcome)
        status_label = "PASS" if outcome["passed"] else "FAIL"
        print(f"{status_label} ({outcome['elapsed_seconds']:.2f}s)")
        if not outcome["passed"]:
            print("-" * 84)
            print(f"FAILURE DETAILS [{suite_id}] (exit code {outcome['returncode']}):")
            if outcome["stdout"].strip():
                print("--- STDOUT ---")
                print(outcome["stdout"].rstrip())
            if outcome["stderr"].strip():
                print("--- STDERR ---")
                print(outcome["stderr"].rstrip())
            print("-" * 84)
            if args.fail_fast:
                break

    total_elapsed = round(time.perf_counter() - overall_start, 3)
    passed_count = sum(1 for item in results if item["passed"])
    failed_count = len(results) - passed_count
    all_passed = failed_count == 0 and len(results) == len(selected)

    print("=" * 84)
    print(f"{'SUITE':<25} {'STATUS':<10} {'TIME (s)':>10}  DESCRIPTION")
    print("-" * 84)
    for item in results:
        st = "PASS" if item["passed"] else "FAIL"
        print(f"{item['suite_id']:<25} {st:<10} {item['elapsed_seconds']:>10.2f}  {item['description']}")
    print("=" * 84)
    verdict = "100% PASS" if all_passed else f"FAILED ({failed_count} failed / {len(results)} run)"
    print(f"SUMMARY: {passed_count}/{len(results)} suites passed in {total_elapsed:.2f}s — {verdict}")
    print("=" * 84)

    if args.json_output:
        report_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "all_passed": all_passed,
            "suite_count": len(results),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "total_elapsed_seconds": total_elapsed,
            "suites": [
                {
                    "suite_id": r["suite_id"],
                    "description": r["description"],
                    "passed": r["passed"],
                    "returncode": r["returncode"],
                    "elapsed_seconds": r["elapsed_seconds"],
                }
                for r in results
            ],
        }
        out_path = Path(args.json_output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report_payload, indent=2) + "\n", encoding="utf-8")

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
