# Task Queue: Dependable Local Brain Pilot

This queue is reconciled with current source, `docs/system-sweep-2026-10-27.md`,
`docs/system-sweep-2026-11-15.md`, and `docs/independent-review-2026-11-15.md`.
Full outcomes, existing behavior, changes, acceptance criteria, dependencies,
verification, and files are in `tasks/plan.md`. Task IDs and statuses are
synchronized across both files.

## Verified foundations — complete

- [x] Durable Markdown atoms, source archive manifest, and rebuildable indexes
  are implemented; smoke and archive/vector canaries pass in `scripts/test_all.py`.
- [x] Review listing and owner transitions `approve`, `reject`, `quarantine`,
  `demote`, `edit`, and two-atom `merge` use the canonical CAS mutator
  (`scripts/atom_mutator.py`), exact source anchors for approvals, history
  backups, audit records, index refresh, and permanently terminal supersession
  with transitive cycle detection.
- [x] Shared structured recall contract (`scripts/recall.py`) powers CLI,
  dashboard, and MCP, supporting `lexical`, `dense`, and `rrf` hybrid search,
  `project:<id>` scoping, hierarchical cross-project link gating, match
  explanations, and read-only duplicate suggestions.
- [x] Native Chroma (`ce48706db17d440fbba04b8c7f86c37e`, `nomic-embed-text`,
  768 dimensions) and SQLite FTS5 are active and verified, with live dynamic
  fallback to FTS5 when the embedding daemon is unreachable.
- [x] Out-of-model epistemic grounding (`scripts/answer_policy.py`) isolates
  `standard` factual answers from `preference` contracts and abstains
  structurally without verified owner-approved claims.

## Ordered active goals

### Goal 0 — Reconcile roadmap with implementation — COMPLETE

- [x] Compare every item in the queue with current source and sweeps; align task
  descriptions with verified behavior.
- [x] Maintain dependency-ordered goals in `tasks/plan.md` and `tasks/todo.md`,
  keeping post-pilot universal host adapters separated under Deferred product
  horizon.
- [x] Verification: plan and queue task IDs, states, order, and release gate
  evidence agree.

### Goal 1 — Curate reviewed examples and version the evaluation set — COMPLETE

- [x] Audit all 14 atom source anchors (`docs/owner-review-batch-proposal-2026-10-27.md`
  and `scripts/audit_sources.py`). Retain the 2 real human owner-approved atoms
  (`structured-logging.md`, `design-for-failure.md`) without fabricating
  unreviewed approvals.
- [x] Present source-backed samples for fact, procedure, and project-decision
  categories while preserving pending status on unreviewed drafts.
- [x] Implement a safe non-factual preference contract
  (`epistemic_contract: owner_subjective_preference` in `scripts/atom_mutator.py`
  and `[Preference]` rendering / strict `standard` vs `preference` mode
  isolation in `scripts/answer_policy.py`).
- [x] Keep `sodium-ion-battery-cost-record.md` pending until its primary article
  is available and reviewed by the owner.
- [x] Bind answerability, expected claims, answer mode, trap rationale, dataset
  version (`2.1.0`), split, and SHA-256 atom revision hashes across all cases.
- [x] Validate every positive answerability label against a current
  owner-approved atom revision in `scripts/evaluation_dataset.py` before scoring.
- [x] Record dataset version/hash and separate candidate-ranking scores from
  approved-answer expected-atom retrieval coverage in
  `scripts/evaluate_retrieval.py`.
- [x] Expand `docs/retrieval-evaluation-proposed.json` (`v2.1.0`) from 7 to 30
  curated cases (10 factual positives, 5 preference, 5 multi-atom exploratory,
  10 adversarial traps) partitioned across `regression` (8), `development` (10),
  and `holdout` (12) splits (`docs/retrieval-evaluation-results-2026-11-15.json`).
- **Depends on:** Goal 0.
- **Details:** `tasks/plan.md` Goal 1.

### Goal 2 — Enforce and measure answer-level grounding — COMPLETE

- [x] Define typed `AnswerRequest`, `GroundedClaim`, `EvidenceReference`, and
  `AnswerResult` contracts in `scripts/answer_policy.py`.
- [x] Validate `standard` factual drafts and `preference` drafts independently
  against exact current approved claims, retrieved atom bytes, canonical
  approvals, and current byte-level source anchors before CLI or Dashboard
  renders output.
- [x] Implement `scripts/evaluate_answers.py` for multi-run ($N \ge 5$)
  end-to-end local model evaluation with P50/P95 latencies, provenance, and
  ignored sidecar output.
- [x] Keep unapproved/exploratory leads and preference contracts strictly
  separated from `standard` factual claims, rendering provenance exclusively
  from program-verified source bytes and escaping Rich/Markdown output.
- [x] Execute $N = 5$ repeated evaluation passes across all 20 scored dataset
  cases (100 runs/model) on both `llama3.2:3b` and `qwen2.5-coder:7b`: achieved
  100.0% claim precision, 100.0% citation grounding, and 0.0% trap leakage
  (`docs/system-sweep-2026-11-15.md`).
- [x] Document pilot corpus scope honestly (2 owner-approved atoms in the local
  vault; additional domain coverage grows only via human owner approvals).
- **Depends on:** Goal 1.
- **Details:** `tasks/plan.md` Goal 2.

### Goal 3 — Complete review and trust maintenance — COMPLETE

- [x] Implement `scripts/audit_sources.py` to report recorded/current source
  hashes, anchor status, atom revision, and current approval (read-only by
  default).
- [x] Support opt-in `--demote-stale --actor <owner>` to demote stale accepted
  atoms via the canonical CAS mutator and append-only decision log.
- [x] Display source path/hash/byte range, current quote/hash, anchor state,
  related atom status, and stale approval demotion in Dashboard Vault Explorer
  using safe non-Markdown code blocks (`st.code`).
- [x] Implement `apply_atom_merge` with CAS on both inputs, bidirectional links,
  backups, paired append-only audit, survivor re-review, permanent terminal lock
  on merged/superseded atoms, and transitive BFS cycle rejection.
- [x] Expose two-atom merge in `scripts/review_atom.py` while keeping
  single-atom transition CLI syntax intact.
- [x] Add owner-facing merge, approve, reject, quarantine, and demote controls
  with explicit consequence warnings in `dashboard/app.py`.
- [x] Verify recall and MCP `get_atom` exclude superseded/merged atoms, and test
  rollback across write, index-refresh, and audit failures in
  `scripts/system_safety_tests.py`.
- **Depends on:** Goals 1–2.
- **Details:** `tasks/plan.md` Goal 3.

### Goal 4 — Close dashboard and MCP access gaps — COMPLETE

- [x] Threat-model local dashboard access (`docs/dashboard-security.md`);
  enforce fail-closed salted `scrypt` password gate, 30-minute idle expiry,
  manual lock, thread-safe cross-session rate-limiting/lockout, strict
  `127.0.0.1` loopback bind (rejecting `0.0.0.0`), and `SameSite=Strict` XSRF
  protection.
- [x] Exercise live non-loopback refusal and cross-origin HTTP/WebSocket upgrade
  penetration probes (`HTTP 403 Forbidden`) in
  `DashboardBoundaryTests.test_non_loopback_binding_and_cross_origin_penetration_rejected`.
- [x] Enforce unconditionally fail-closed bearer token authentication ($\ge 16$
  chars, `0600` token store, pre-audit/pre-mkdir verification) and loopback
  transport validation across all MCP JSON-RPC handlers (`scripts/mcp_server.py`,
  `brain/mcp-config.md`).
- [x] Restrict MCP atom reads strictly to current non-superseded `accepted`
  atoms and isolate `add_to_inbox` writes inside `brain/0-raw/inbox/` with
  path-traversal, control-character, and payload-size sanitization.
- **Depends on:** Goal 3.
- **Details:** `tasks/plan.md` Goal 4.

### Goal 5 — Make setup, health, and recovery reproducible — COMPLETE

- [x] Formalize `pyproject.toml` and `uv.lock` for Python 3.11 with core +
  `dashboard`, `online`, `mcp`, `scout`, `watchdog`, `evaluation`, and `dev`
  extras, verified via `uv sync --frozen`.
- [x] Provide verified cross-platform installers (`setup.ps1` and `setup.sh`)
  that inspect OS/CPU/RAM, C++ ABI (`vcruntime140.dll` / `libstdc++`), and GPU
  drivers, synchronize locked dependencies, initialize vault/FTS5 indexes, and
  preserve Git repository boundaries (`setup.ps1 -NoLaunch` verified exit `0`).
- [x] Upgrade `second-brain doctor` (`scripts/cli.py`) and `scripts/doctor.py`
  to report OS/CPU/RAM, C++ runtime, GPU profile, optional modules, Ollama
  models, lexical status, vector generation/dimensions, atom/approval counts,
  and quarantined inbox counts.
- [x] Document and verify clean setup, offline lexical degradation, deterministic
  staged vector rebuilds (with backup generation pruning and handle cleanup),
  and disaster recovery in `docs/recovery-and-operations-playbook.md`.
- **Depends on:** Goals 2–3.
- **Details:** `tasks/plan.md` Goal 5.

### Goal 6 — Expose project-scoped recall consistently — COMPLETE

- [x] Standardize `project:<id>` context tags and expose project, cross-project
  link, and status filters consistently across CLI (`chat_with_memory.py`),
  Dashboard (`dashboard/app.py`), and MCP (`scripts/mcp_server.py`).
- [x] Render concrete lexical, dense, RRF hybrid, context, and linked-neighbor
  match explanations in `RecallResult.match_reasons`.
- [x] Add read-only duplicate candidate suggestions (`suggest_duplicate_atoms`
  in `scripts/recall.py`) with revision hashes and provenance without automatic
  mutation.
- [x] Verify consistent project isolation and offline FTS5 fallback across all
  interfaces in `scripts/system_safety_tests.py`.
- **Depends on:** Goals 2 and 5.
- **Details:** `tasks/plan.md` Goal 6.

### Goal 7 — Expand benchmark and decide retrieval modes by evidence — COMPLETE

- [x] Evaluate `lexical` (SQLite FTS5), `dense` (Chroma cosine), and `rrf`
  (Reciprocal Rank Fusion) across `regression`, `development`, and `holdout`
  splits on the 30-case dataset (`docs/retrieval-evaluation-results-2026-11-15.json`),
  achieving `Recall@5 = 1.000` across all categories and `0/10` trap false
  positives.
- [x] Record Recall@5, MRR, nDCG@5, P50/P95 retrieval and answer latencies, and
  dataset/atom/model/generation SHA-256 provenance (`docs/system-sweep-2026-11-15.md`).
- [x] Retain model-free `lexical` FTS5 as the zero-dependency default (`6.56 ms`
  P50 vs `63.16 ms` dense / `70.06 ms` RRF) with opt-in `rrf`/`dense` and
  automatic FTS5 fallback when Ollama is offline.
- **Depends on:** Goals 1, 2, 5, and 6.
- **Details:** `tasks/plan.md` Goal 7.

### Goal 8 — Independent review and local pilot release gate — COMPLETE

- [x] Execute an isolated read-only adversarial architecture and security audit
  (`2a4e3faa-6244-4b81-8c9d-2b6e8a184782`) across all repository modules,
  scripts, configs, and docs (`docs/independent-review-2026-11-15.md`).
- [x] Reconcile 100% of Critical, High, Medium, Low, and Informational findings
  in code and documentation with dedicated regression tests
  (`IndependentAuditReconciliationTests`) and perform primary-agent self-audit.
- [x] Unify all diagnostic, dataset, provenance, unit/penetration, Chroma
  rebuild, and end-to-end smoke suites in `scripts/test_all.py` and verify
  `6/6` suites pass (`100% PASS`).
- **Depends on:** Goals 1–7.
- **Details:** `tasks/plan.md` Goal 8.

## Deferred product horizon

- **Post-Pilot Universal Host Ecosystem:** Host-independent memory API, external
  online/owner-built AI host adapters with per-host sharing consent, and signed
  OS-native GUI installers build on top of the completed Goal 8 local pilot
  core (`tasks/plan.md`, `CODEX_CLI_GOAL.md`).
- **Comparative External SOTA Benchmarking:** Benchmark against third-party
  memory systems on large multi-hundred-atom owner-reviewed corpora and declared
  GPU/CPU hardware profiles before making external state-of-the-art marketing
  claims.
