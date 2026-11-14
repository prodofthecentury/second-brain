# Implementation Plan: Dependable Local Brain Pilot

**Plan status:** COMPLETE (Phase 1 Local Pilot + Phase 2 Universal Brain Plugin & Cognitive Core Release Gate Passed — 2026-11-15).
Reconciled with current source, `docs/system-sweep-2026-10-27.md`,
`docs/system-sweep-2026-11-15.md`, `docs/independent-review-2026-11-15.md`, and
`docs/phase2-architecture-and-sweep-2026-11-15.md`.

## Verified release baseline (2026-11-15)

- **Vault & Provenance:** 14 canonical Markdown atoms in `brain/2-atoms/`; 2
  current human owner-approved atoms (`structured-logging.md`,
  `design-for-failure.md`) with verified SHA-256 byte-offset source anchors.
  `sodium-ion-battery-cost-record.md` and unreviewed drafts remain `pending` per
  the strict non-fabrication invariant.
- **Disposable Indexes & Hybrid RRF:** Native Chroma cosine generation
  `ce48706db17d440fbba04b8c7f86c37e` (`nomic-embed-text`, 768 dimensions, 14/14
  atoms indexed) and SQLite FTS5 (`brain/.sidecar/lexical.sqlite3`) are current.
  `scripts/recall.py` supports `lexical`, `dense`, and Reciprocal Rank Fusion
  (`rrf`), `project:<id>` scoping, hierarchical cross-project link gating, and
  automatic dynamic fallback to FTS5 with explicit `[WARNING]` diagnostics when
  Ollama is offline.
- **Curated 30-Case Benchmark (`v2.1.0`):** `docs/retrieval-evaluation-proposed.json`
  covers 10 factual positives, 5 preference contracts, 5 multi-atom exploratory
  cases, and 10 adversarial traps across `regression` (8), `development` (10),
  and `holdout` (12) splits (`docs/retrieval-evaluation-results-2026-11-15.json`):
  `Recall@5 = 1.000` across every category and split, and `0/10` trusted trap
  false positives across all rankers.
- **Multi-Model Grounded Answer Evaluation ($N = 5$):** 100 end-to-end runs per
  model on `llama3.2:3b` and `qwen2.5-coder:7b` achieved **100.0% claim
  precision**, **100.0% citation grounding**, and **0.0% adversarial trap
  leakage** (`30.0%` vs `94.0%` positive coverage on CPU).
- **Security & Trust Boundaries:** CAS mutations (`scripts/atom_mutator.py`)
  lock merged/superseded atoms permanently and block transitive merge cycles;
  legacy bypass scripts (`ollama_refinery.ps1`, `chat_with_memory.ps1`) delegate
  strictly to Python CAS/grounding pipelines; MCP (`scripts/mcp_server.py`)
  enforces fail-closed bearer auth before audit/mkdir and isolates captures in
  `brain/0-raw/inbox/`; Dashboard (`scripts/dashboard_auth.py`,
  `dashboard/app.py`) enforces `127.0.0.1` loopback, salted `scrypt` login,
  thread-safe lockout, `SameSite=Strict` XSRF cookies, and `HTTP 403 Forbidden`
  cross-origin WebSocket rejection; Scout (`scripts/ollama_scout.py`) pins DNS
  and blocks loopback, RFC 1918, and NAT64 ranges.
- **Reproducible Packaging & CI Gate:** `pyproject.toml` + `uv.lock` lock all
  core and 7 optional extras (`124` packages); `setup.ps1` and `setup.sh` verify
  host capabilities without touching Git; historical commit `bffca9e` is audited
  (`docs/git-history-audit-2026-11-15.md`) with a clean `release-code-only`
  branch ready; `scripts/test_all.py` passes **7/7 suites (`100% PASS`)**.
- **Phase 2 Universal Host & Cognitive Core:** Hardware-tiered capability
  profiling (`scripts/hardware_profile.py`), CAS-locked quarantined inbox
  promotion (`scripts/inbox_workflow.py`), per-host online/local AI adapters
  with DNS-pinned HTTPS, multi-project containment, and secret/PII redaction
  (`scripts/host_adapters.py`), zero-churn SQLite WAL Cognitive Core telemetry
  and `brain/4-experiences/` episode logs (`scripts/cognitive_core.py`), and
  bearer-authenticated `127.0.0.1` HTTP Loopback Memory API + `MemoryApiClient`
  SDK (`scripts/memory_api.py`) are verified across all 12 independent security
  audit checks (`scripts/phase2_safety_tests.py`).

## Architecture and trust rules

- Markdown atoms and immutable evidence files remain durable source of truth;
  lexical/vector indexes are disposable projections.
- Model output never approves, merges, deletes, or promotes memory. Owner
  decisions remain explicit, hash-bound, and auditable.
- `standard` answers state only current owner-reviewed factual claims;
  `preference` answers render only `epistemic_contract: owner_subjective_preference`
  claims prefixed with `[Preference]` and never bleed into `standard` mode.
- Citation integrity proves that a quote is anchored to bytes; it does not
  prove that the source entails the claim.
- Any answer claim and citation must be validated outside the model before
  display. If validation fails, suppress the invalid claim or abstain.
- Keep lexical retrieval available without an embedding service; degrade
  dynamically to FTS5 with explicit warnings if dense embeddings are offline.
- Dashboard and MCP remain loopback-only (`127.0.0.1`) and fail-closed on
  missing/invalid authentication.
- Never fabricate owner approvals, source hashes, or byte offsets.

## Dependency-ordered tasks

### Goal 0 — Reconcile roadmap with implementation — COMPLETE

**Outcome:** `tasks/plan.md` and `tasks/todo.md` reflect verified repository
behavior and synchronized task states.

**Verification:** Audited against source, `docs/system-sweep-2026-10-27.md`, and
`docs/system-sweep-2026-11-15.md`.

**Files:** `tasks/plan.md`, `tasks/todo.md`.

### Goal 1 — Curate reviewed examples and a versioned evaluation set — COMPLETE

**Outcome:** Safe non-factual preference contract (`owner_subjective_preference`)
implemented; dataset `second-brain-retrieval-curated-30` (`v2.1.0`) expanded to
30 curated cases across `regression` (8), `development` (10), and `holdout` (12)
splits with SHA-256 revision bindings and zero fabricated approvals.

**Verification:** `uv run python scripts/evaluation_dataset.py` (`PASS`, 30
cases, non-empty splits, SHA-256 revision verification) and
`scripts/audit_sources.py` (`PASS`).

**Files:** `docs/retrieval-evaluation-proposed.json`,
`scripts/evaluation_dataset.py`, `scripts/atom_mutator.py`,
`scripts/memory_format.py`, `scripts/answer_policy.py`.

### Goal 2 — Enforce and measure answer-level grounding — COMPLETE

**Outcome:** Typed `standard` and `preference` grounding contracts enforce 100%
verbatim claim whitelisting and byte-offset citation grounding across CLI and
Dashboard, verified across $N = 5$ passes on `llama3.2:3b` and
`qwen2.5-coder:7b` (`100.0%` precision, `100.0%` citation grounding, `0.0%` trap
leakage).

**Verification:** `scripts/evaluate_answers.py` (`100` runs/model),
`AnswerPolicyTests` and `IndependentAuditReconciliationTests` in
`scripts/system_safety_tests.py`.

**Files:** `scripts/answer_policy.py`, `scripts/evaluate_answers.py`,
`chat_with_memory.py`, `dashboard/app.py`.

### Goal 3 — Complete routine review and trust maintenance — COMPLETE

**Outcome:** Source-anchor auditing (`scripts/audit_sources.py`), owner-directed
CAS transitions, two-atom merge (`apply_atom_merge`) with permanent terminal
supersession and transitive BFS cycle detection, and Dashboard review/merge UI
controls operate atomically under `writer.lock`.

**Verification:** `AtomMutationSafetyTests`, `AuditSourcesTests`, and
`test_merged_atoms_are_permanently_terminal_against_demote_edit_and_cycles` in
`scripts/system_safety_tests.py`.

**Files:** `scripts/atom_mutator.py`, `scripts/audit_sources.py`,
`scripts/review_atom.py`, `scripts/memory_review.py`, `dashboard/app.py`.

### Goal 4 — Close dashboard and MCP access gaps — COMPLETE

**Outcome:** Dashboard enforces `127.0.0.1` loopback, salted `scrypt` password
auth, thread-safe lockout, `SameSite=Strict` XSRF cookies, `st.code` untrusted
previews, and cross-origin WebSocket `403` rejection. MCP enforces fail-closed
bearer token auth before audit/mkdir, restricts reads to current non-superseded
`accepted` atoms, and quarantines `add_to_inbox` writes in `brain/0-raw/inbox/`.

**Verification:** `DashboardBoundaryTests`, `MCPBoundaryTests`, and
`ScoutPinningTests` in `scripts/system_safety_tests.py`.

**Files:** `scripts/dashboard_auth.py`, `dashboard/app.py`,
`scripts/mcp_server.py`, `scripts/ollama_scout.py`, `docs/dashboard-security.md`,
`brain/mcp-config.md`.

### Goal 5 — Make setup, health, and recovery reproducible — COMPLETE

**Outcome:** Locked Python 3.11 environment (`pyproject.toml` + `uv.lock` with 7
extras), cross-platform `setup.ps1` / `setup.sh` installers with hardware/ABI
checks, comprehensive `doctor` diagnostics, staged Chroma generation pruning and
handle cleanup, and `docs/recovery-and-operations-playbook.md`.

**Verification:** `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoLaunch`
(exit `0`), `uv sync --frozen`, `scripts/chroma_rebuild_canary.py`, and
`scripts/doctor.py`.

**Files:** `pyproject.toml`, `uv.lock`, `setup.ps1`, `setup.sh`,
`scripts/cli.py`, `scripts/doctor.py`, `scripts/vector_store.py`,
`scripts/rebuild_vectors.py`, `docs/recovery-and-operations-playbook.md`.

### Goal 6 — Expose project-scoped recall consistently — COMPLETE

**Outcome:** `project:<id>` context filtering, cross-project `[[wiki-link]]`
gating, concrete match explanations, and read-only duplicate candidate
suggestions (`suggest_duplicate_atoms`) operate consistently across CLI,
Dashboard, and MCP, including offline FTS5 mode.

**Verification:** `ProjectScopingAndHybridRRFTests` in
`scripts/system_safety_tests.py`.

**Files:** `scripts/recall.py`, `chat_with_memory.py`, `dashboard/app.py`,
`scripts/mcp_server.py`.

### Goal 7 — Expand and decide retrieval modes by evidence — COMPLETE

**Outcome:** Benchmarked `lexical`, `dense`, and `rrf` on the 30-case dataset
across `regression`, `development`, and `holdout` splits (`Recall@5 = 1.000`,
`0/10` trap false positives, P50/P95 latencies persisted in
`docs/retrieval-evaluation-results-2026-11-15.json`); retained fast zero-model
`lexical` (`6.56 ms` P50) as default with opt-in `rrf`/`dense` and verified live
Ollama-kill fallback.

**Verification:** `scripts/evaluate_retrieval.py` and live daemon-kill test
recorded in `docs/system-sweep-2026-11-15.md`.

**Files:** `scripts/recall.py`, `scripts/evaluate_retrieval.py`,
`docs/retrieval-evaluation-proposed.json`,
`docs/retrieval-evaluation-results-2026-11-15.json`.

### Goal 8 — Independent review and local pilot release gate — COMPLETE

**Outcome:** Isolated read-only adversarial audit
(`2a4e3faa-6244-4b81-8c9d-2b6e8a184782`) completed; all 15 findings reconciled
in code/docs (`docs/independent-review-2026-11-15.md`); unified CI gate
`scripts/test_all.py` passes **6/6 suites (`100% PASS`)**.

**Verification:** `uv run python scripts/test_all.py` (`6/6 PASS` in `53.08s`,
`66/66` safety tests `PASS`).

**Files:** `scripts/test_all.py`, `scripts/system_safety_tests.py`,
`docs/independent-review-2026-11-15.md`, `docs/system-sweep-2026-11-15.md`.

### Goal 9 — Hardware-tiered capability profiles — COMPLETE

**Outcome:** Deterministic OS/CPU/RAM/VRAM/C++ ABI inspection and loopback-only
Ollama probing (`scripts/hardware_profile.py`) classify hosts into
`tier_0_lexical_only`..`tier_3_gpu_accelerated` with explicit retrieval/answer
SLOs and model recommendations across CLI, `doctor`, installers, Dashboard, and
Loopback API.

**Verification:** `HardwareProfileTests` and `Phase2AuditReconciliationTests` in
`scripts/phase2_safety_tests.py`, `second-brain profile`, and
`setup.ps1 -NoLaunch`.

**Files:** `scripts/hardware_profile.py`, `scripts/cli.py`, `scripts/doctor.py`,
`setup.ps1`, `setup.sh`, `dashboard/app.py`.

### Goal 10 — Quarantined inbox promotion and multi-atom Refinery — COMPLETE

**Outcome:** Untrusted captures in `brain/0-raw/inbox/` undergo deterministic
risk scanning, CAS-locked owner promotion or rejection with frontmatter/HTML
comment neutralization, beacon defanging, and origin anti-laundering
(`scripts/inbox_workflow.py`), feeding verbatim-quote multi-atom Refinery
extraction (`scripts/ollama_refinery.py`).

**Verification:** `QuarantinedInboxPromotionTests` and `test_high01_*` in
`scripts/phase2_safety_tests.py`.

**Files:** `scripts/inbox_workflow.py`, `scripts/memory_format.py`,
`scripts/ollama_refinery.py`, `dashboard/app.py`.

### Goal 11 — Per-host online and custom AI adapters — COMPLETE

**Outcome:** Explicit per-host consent (`host-sharing-policy.json`), multi-project
`all(...)` scope containment, outbound credential blocking and PII redaction,
DNS-pinned `https://...:443` transport (`PinnedHTTPTransport`), and out-of-model
grounding (`scripts/answer_policy.py`) unify `ollama_local`,
`owner_custom_local`, `openai_online`, `anthropic_online`, and `gemini_online`.

**Verification:** `HostAdapterConsentAndRedactionTests`, `test_critical01_*`,
and `test_high03_*` in `scripts/phase2_safety_tests.py`.

**Files:** `scripts/host_adapters.py`, `chat_with_memory.py`,
`dashboard/app.py`, `scripts/memory_api.py`.

### Goal 12 — Cognitive Core telemetry, fitness, and experience episodes — COMPLETE

**Outcome:** Disposable SQLite WAL telemetry (`brain/.sidecar/telemetry.sqlite3`)
tracks retrieval/citation/bottleneck/feedback fitness and procedural skill
maturity (`procedural_draft` -> `verified_procedure` -> `skill_candidate`) with
zero canonical atom hash churn, alongside create-only `brain/4-experiences/`
records and quarantined procedural inbox proposals (`scripts/cognitive_core.py`).

**Verification:** `CognitiveCoreTests` in `scripts/phase2_safety_tests.py`.

**Files:** `scripts/cognitive_core.py`, `scripts/config.py`, `dashboard/app.py`,
`chat_with_memory.py`, `scripts/memory_api.py`.

### Goal 13 — Loopback Memory API, SDK, and Phase 2 security audit — COMPLETE

**Outcome:** Multi-threaded `127.0.0.1` HTTP JSON service, typed
`MemoryApiClient` SDK, and IDE/host adapter descriptors (`scripts/memory_api.py`)
enforce bearer auth, DNS-rebinding `Host` checks, `Origin`/`Sec-Fetch-Site`
CSRF rejection, socket read timeouts, and non-lockout valid-token protection;
all 12 findings from independent `security-auditor` session
`b8248c56-e4ba-423f-85d9-1fda32190901` are remediated and verified in the `7/7`
unified CI gate (`scripts/test_all.py`).

**Verification:** `uv run --frozen python scripts/test_all.py` (`7/7 PASS`,
`100% PASS`).

**Files:** `scripts/memory_api.py`, `scripts/phase2_safety_tests.py`,
`scripts/test_all.py`, `docs/phase2-architecture-and-sweep-2026-11-15.md`.

## Deferred product horizon

Goals 1–13 complete the local pilot, universal loopback API, per-host online
consent/redaction adapters, quarantined inbox promotion workflow, hardware-tiered
profiling, and Cognitive Core layer. Remaining post-Phase-2 items are OS-signed
desktop GUI installers (MSI/DMG/AppImage) and large-corpus comparative external
SOTA benchmarks (`CODEX_CLI_GOAL.md`).

## Checkpoints

- **Goals 1–2 (Passed):** Curated 30-case dataset validated; grounding enforces
  100% claim precision and 0% trap leakage.
- **Goals 3–5 (Passed):** CAS terminal supersession, loopback/auth boundaries,
  cross-platform installers, and index recovery playbooks verified.
- **Goals 6–7 (Passed):** Project-scoped recall, RRF hybrid fusion, and live
  offline FTS5 degradation verified.
- **Goal 8 (Passed):** Independent adversarial review reconciled and
  `scripts/test_all.py` achieves 100% pass rate.

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Labels expand faster than owner review | High | Positive labels require current SHA-256 approved atom revisions (`evaluation_dataset.py`) |
| Citation is mistaken for entailment | High | Validate exact claims outside the model and separately audit source byte anchors |
| Interface hardening locks out local users | Medium | Salted `scrypt` CLI password reset (`dashboard_auth.py set-password`) & loopback policy |
| Dependency lock omits native runtime needs | High | `uv.lock` + `setup.ps1`/`setup.sh` C++ ABI checks (`vcruntime140.dll`/`libstdc++`) |
| Dense embedder offline drops user queries | Medium | Automatic dynamic fallback to SQLite FTS5 with explicit `[WARNING]` banner |
| Legacy history leaked on future remote push | High | Audited `bffca9e` and prepared clean `release-code-only` orphan branch |
