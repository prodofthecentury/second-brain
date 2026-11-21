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

### Goal 14 — Context/Fitness Ranking, Skill Playbooks & Grounded Executive Planner (Phase 3A) — COMPLETE

**Why:** Extend Cognitive Core Layers 4–5 with fitness-aware ranking, bounded working-context assembly, owner-gated CAS Skill Playbooks under `brain/5-playbooks/`, and a citation-verified Executive Planner, locked down by an independent `security-auditor` sweep.

**What was built:**
1. Context-profile and fitness-aware ranking (`scripts/recall.py`: `best_bm25` over validated candidates, utility excluding raw `retrieval_hits`, direct `item.content` hashing in `assemble_bounded_working_context`) plus durable correction logging in `brain/0-raw/archive/atom-corrections.jsonl` (`scripts/cognitive_core.py`).
2. Owner-gated Skill Playbooks (`scripts/skill_playbooks.py`, `brain/5-playbooks/`, `playbook-decisions.jsonl`, `.history/` backups): CAS re-promotion (`expected_playbook_sha256`), per-atom project scope containment (`allow_multi_project`, `playbook_visible_in_scope`), whole-Claim step grounding (`_claim_grounding_units`), fail-closed `verify_playbook_integrity`, and frontmatter-only deprecation.
3. Grounded Executive Planner (`scripts/executive_planner.py`, `POST /v1/plan`, `GET /v1/playbooks`, `second-brain playbooks` / `plan`, Dashboard tabs): per-atom host scope containment, scoped pending candidate counts, full-Playbook expansion in `model_assisted` mode (`EXECUTIVE_PLAN_DRAFT_SCHEMA`), and regression tests for `H1`, `H2`, `M1`–`M4`, `L1`–`L7` in `scripts/phase3_cognitive_tests.py` (13 tests).

**Files:** `scripts/recall.py`, `scripts/cognitive_core.py`, `scripts/skill_playbooks.py`, `scripts/executive_planner.py`, `scripts/memory_api.py`, `scripts/cli.py`, `scripts/doctor.py`, `dashboard/app.py`, `scripts/phase3_cognitive_tests.py`, `docs/phase3a-and-phase4-architecture-2026-11-15.md`.

### Goal 15 — Scale Engine, Zero-Friction Notes, Excerpt Verification & Bulk Importer (Phase 4) — COMPLETE

**Why:** Scale lexical indexing and provenance verification to multi-thousand-note corpora with millisecond freshness/query paths while enabling instant owner note capture and one-click promotion to cryptographically anchored atoms.

**What was built:**
1. Stat-cached `os.scandir()` revision manifests (`scripts/index_manifest.py` above `LARGE_VAULT_STAT_CACHE_THRESHOLD = 64`), O(|changed|) `upsert_lexical_atoms` / `sync_lexical_index` / paginated `list_indexed_atoms_fast` (`scripts/lexical_store.py`), tail-cached `latest_decisions_by_atom` (`scripts/atom_mutator.py`), SHA-256 cached source normalization (`scripts/evidence.py`), and synthetic corpus benchmarking (`scripts/scale_benchmark.py`, `second-brain benchmark`).
2. Zero-friction working notes (`scripts/notes_store.py`, `brain/notes/`, `brain/.sidecar/notes.sqlite3`) with CAS updates/deletes, quarantined risky captures, and `verify_note_excerpt()` creating immutable raw snapshots under `brain/0-raw/note-snapshots/` before anchoring and owner approval.
3. Idempotent Markdown/Obsidian vault importer (`scripts/bulk_import.py`, `second-brain import`).

**Files:** `scripts/index_manifest.py`, `scripts/lexical_store.py`, `scripts/atom_mutator.py`, `scripts/evidence.py`, `scripts/notes_store.py`, `scripts/bulk_import.py`, `scripts/scale_benchmark.py`, `scripts/rebuild_vectors.py`.

### Goal 16 — Streaming Chat, Dual-Token Product API & IDE/Antigravity Connectors (Phase 4) — COMPLETE

**Why:** Deliver low-latency `instant` / `verified` / `stream` chat over verified atoms and working notes, strict owner-vs-agent token separation on the Loopback HTTP API, and one-click MCP setup for local IDEs and agent hosts.

**What was built:**
1. Streaming/instant/verified chat (`scripts/streaming_chat.py`, `second-brain chat`, `POST /v1/chat/stream`, `POST /v1/chat/warmup`) with out-of-model citation checks and claim validation.
2. Dual-token Loopback Product API (`scripts/api_product.py` dispatched from `scripts/memory_api.py`): gating notes write/verify, review approvals, inbox triage, and bulk imports behind `owner_authorized` while allowing agent tokens read recall/notes and quarantined `/v1/remember` capture.
3. One-click host connector (`scripts/ide_connector.py`, `second-brain connect`), plugin bundles (`plugins/antigravity/second-brain/`, `plugins/claude-code/second-brain/`), Electron desktop companion (`desktop/`), VS Code / Cursor / Windsurf extension (`extensions/vscode-second-brain/`), Windows release signing script (`scripts/sign_release.ps1`), custom planner schema support (`scripts/host_adapters.py`), MCP note/remember tools paired with `trusted_stdio_session()` (`scripts/mcp_server.py`), and 32 product/connector/security regression tests in `scripts/phase4_product_tests.py` (including Phase 4 audit remediations `P4-HIGH-01`..`P4-MED-04` and Phase 5 follow-ups) inside the **11-suite unified test gate** (`scripts/test_all.py`).

**Files:** `scripts/streaming_chat.py`, `scripts/api_product.py`, `scripts/ide_connector.py`, `scripts/mcp_server.py`, `scripts/host_adapters.py`, `scripts/sign_release.ps1`, `desktop/`, `extensions/vscode-second-brain/`, `plugins/`, `scripts/phase4_product_tests.py`, `scripts/test_all.py`, `scripts/system_safety_tests.py`.

### Goal 17 — OS-Signed Desktop Installers, SOTA Benchmark & Phase 5 Verifiable Vault Suite (`v0.2.0`) — COMPLETE

**Why:** Close the remaining product horizon items (`CODEX_CLI_GOAL.md`) with native cross-platform OS-signed desktop installers, comparative external SOTA benchmarking on multi-hundred-atom corpora, and cryptographic Merkle proofs, Contradiction Radar, and Time-Travel snapshots over verified vault state.

**What was built:**
1. Cross-platform OS-signed desktop installer pipeline (`nsis`, `msi`, `dmg`/`zip`, `AppImage`/`deb` in `desktop/package.json`), unified `.exe`/`.msi`/`.whl`/`.vsix` packaging & Authenticode signing (`scripts/package_desktop.ps1`, `scripts/package_desktop.sh`, `scripts/sign_release.ps1`), user-writable packaged runtime bootstrap (`ensureBackendBootstrapped` + bundled `setup.ps1`/`setup.sh`), 8-tab Electron UI (`Playbooks & Plan` + `Vault Proof & Radar`), and live tray lifecycle controls (`Start`/`Restart`/`Stop Memory API`, `Open Vault Folder`, `Launch at Login`) in `desktop/main.js`.
2. Comparative SOTA benchmark harness (`scripts/sota_benchmark.py`, `second-brain sota-benchmark`) evaluating Second Brain against naive BM25 RAG, unscoped flat memory, and ungrounded LLM synthesis across positive recall, claim precision, citation grounding, pending/superseded trap leakage, cross-project isolation, and abstention accuracy with declared hardware profiles.
3. Cryptographic Vault Merkle Proof & Audit Ledger (`scripts/vault_ledger.py`, `second-brain ledger`, `/v1/ledger/*`), Contradiction & Drift Radar + Knowledge Graph (`scripts/knowledge_graph.py`, `second-brain graph`, `/v1/graph/*`), and Time-Travel Vault Snapshots, Semantic Diff & Point-in-Time Recall (`scripts/time_travel.py`, `second-brain snapshot`, `/v1/snapshots/*`), surfaced across Electron, Streamlit (`dashboard/app.py`), and VS Code (`extensions/vscode-second-brain/extension.js`) and verified by `scripts/phase5_god_features_tests.py` inside the **12-suite unified release gate** (`scripts/test_all.py`, `12/12 PASS`).

**Verification:** `uv run --locked python scripts/test_all.py` (`12/12 PASS`, `100% PASS`).

**Files:** `desktop/package.json`, `desktop/main.js`, `desktop/renderer/index.html`, `desktop/renderer/app.js`, `desktop/renderer/styles.css`, `dashboard/app.py`, `extensions/vscode-second-brain/extension.js`, `extensions/vscode-second-brain/package.json`, `docs/sota-benchmark-results-2026-11-15.json`, `scripts/package_desktop.ps1`, `scripts/package_desktop.sh`, `scripts/sign_release.ps1`, `scripts/sota_benchmark.py`, `scripts/vault_ledger.py`, `scripts/knowledge_graph.py`, `scripts/time_travel.py`, `scripts/api_product.py`, `scripts/memory_api.py`, `scripts/cli.py`, `scripts/phase5_god_features_tests.py`.

### Goal 18 — Core Runtime Parity, Three-Store Durability, Security Redaction & Sub-15ms Recall — COMPLETE

**Why:** Eliminate split-brain vault/port discovery across Desktop, CLI, Dashboard, and MCP; enforce strict Three-Store durability (`0-raw/archive/` vs disposable `.sidecar/`) with `O(changed)` content-addressed snapshots; harden non-owner snapshot/audit redaction and spec-compliant YAML frontmatter parsing; and achieve `<= 15 ms` P95 recall and paginated API reads with `0/10` trap false positives.

**What was built:**
1. Unified `vault-path.txt` & `api-port` discovery across `scripts/config.py`, `scripts/cli.py`, `scripts/memory_api.py`, `scripts/mcp_server.py`, `scripts/ide_connector.py`, `desktop/main.js`, `extensions/vscode-second-brain/extension.js`, and `dashboard/app.py`, plus full CLI/README parity (`mcp --generate-token`, `capture --tags`, `notes add/verify`, `import --overwrite-modified`) and loopback no-proxy/no-redirect setup probes (`setup.ps1`, `setup.sh`).
2. Durable Three-Store migration of `vault-ledger.jsonl` and content-addressed `schema_version: 3` snapshot blobs into `brain/0-raw/archive/` (with transparent `.sidecar/` fallback), generalized atom+playbook Merkle proofs (`prove_leaf_membership`), Porter-stemmed BM25 recall, spec-compliant YAML/JSON frontmatter parsing, verified note `contexts:` propagation, durable `atom-corrections.jsonl` fitness merging (`ret_hits` loop removed), and strict non-owner revision/evidence/playbook redaction.
3. Bounded `ThreadPoolExecutor` + SSE stream concurrency guards on `MemoryLoopbackHTTPServer`, FTS5-backed `GET /v1/atoms`, SHA-256-cached anchor/metadata verification, single-term (`min(2, len(query_terms))`) + calibrated dense/RRF synonym recall (`Recall@5 = 1.000`, `0/10` trap false positives, sub-15ms P95), and relocated operator runbooks (`docs/runbooks/`).

**Verification:** `scripts/evaluate_retrieval.py` (`Recall@5 = 1.000`, `0/10` trap false positives), `second-brain doctor` (`exit 0`), and `scripts/test_all.py` (`18/18 PASS`, `100% PASS`).

### Goal 19 — Document Ingestion, Chunked Refinery, Incremental Vectors, Research View, Multi-Host DLP Chat, Semantic NLI Radar & 17-Tool MCP — COMPLETE

**Why:** Support multi-format local document ingestion with cryptographic byte-offset anchoring, context-window-safe Refinery extraction, incremental vector carry-forward, deterministic `"research"` leads, multi-host DLP streaming chat, semantic NLI contradiction detection, 17-tool MCP parity, and 6-host rule-installed IDE connectors.

**What was built:**
1. Local document extraction & bulk import (`scripts/doc_extractors.py`, `scripts/ingest_multimodal.py`, `scripts/bulk_import.py`) for `.pdf`, `.docx`, `.epub`, `.html`, `.csv`, and `.tsv` with immutable binary archival, `ARCHIVE_MANIFEST` SHA-256 lineage, `[page N]` coordinate spans, `[MM:SS.mmm -> MM:SS.mmm]` Whisper timestamps, and fail-closed Pillow image checks.
2. Context-window-safe Refinery chunking (`scripts/ollama_refinery.py`), incremental vector carry-forward (`scripts/rebuild_vectors.py`), batched atomic `JsonVectorCollection` writes (`scripts/vector_store.py`), hardware-gated watchdog daemon with debounced FTS5 sync (`scripts/watchdog_daemon.py`), honest Editor deferral & project scope partitioning (`scripts/ollama_editor.py`), 8-field `ExperienceRecord` spec (`scripts/cognitive_core.py`), and structured action-item extraction (`scripts/notes_store.py`).
3. `"research"` answer mode with deterministic `[UNVERIFIED LEADS]` (`scripts/answer_policy.py`), multi-host DLP streaming chat with DNS pinning and PII redaction/rehydration (`scripts/streaming_chat.py`, `scripts/host_adapters.py`), semantic/NLI Contradiction & Drift Radar (`scripts/knowledge_graph.py`), 17-tool MCP server (`scripts/mcp_server.py`), 6-host rule-installed IDE connectors (`scripts/ide_connector.py`), and 9-system comparative SOTA benchmark (`scripts/sota_benchmark.py`, `docs/sota-benchmark-results-2026-11-19.json`), verified by `scripts/goal2_completion_tests.py`.

### Goal 20 — Full Desktop/VS Code/Dashboard UI Parity, Clean Wheel Namespace, Offline Wheelhouse & 18-Suite Release Gate — COMPLETE

**Why:** Bring full UI parity across Electron Desktop, VS Code, and Streamlit Dashboard, package a clean `second_brain` wheel namespace with offline wheelhouse and inner+outer Authenticode signing, and verify every frontend and packaging boundary in CI.

**What was built:**
1. Complete Phase 4/5 and Goal 2 UI parity across Electron Desktop (`desktop/`), VS Code extension (`extensions/vscode-second-brain/`), and Streamlit Dashboard (`dashboard/app.py`), including `"research"` mode & `[UNVERIFIED LEADS]`, multi-host streaming chat, document import, Merkle proof verification, Contradiction Radar, and Time-Travel snapshots (`docs/goal3-api-contract.md`).
2. Clean `second_brain` Python wheel namespace (`second_brain/`), offline wheelhouse builder (`scripts/build_wheelhouse.py`), pure-Python `.vsix` builder (`scripts/build_vsix.py`), multi-format desktop icon generator (`scripts/make_icons.py`), and inner+outer Authenticode signing (`desktop/build/sign.js`, `scripts/sign_release.ps1`).
3. Automated behavioral test suites for Electron Desktop DOM/XSS/graph/pagination (`scripts/run_desktop_tests.py`), VS Code extension SSE/singleton/governance (`scripts/run_vscode_tests.py`), Streamlit AppTest dashboard (`scripts/dashboard_apptest_tests.py`), backend contracts (`scripts/goal3_backend_contract_tests.py`), and clean wheel/VSIX/wheelhouse packaging (`scripts/wheel_packaging_tests.py`), integrated into the **18-suite unified release gate** (`scripts/test_all.py`, `18/18 PASS`).

### Goal 21 — Zero-Knowledge E2EE Multi-Device Vault Sync, Full 3-Surface UI Parity & 19-Suite Gate (`v0.3.0`) — COMPLETE

**Why:** Enable private multi-device vault synchronization without leaking plaintext metadata or breaking Three-Store isolation, bring full UI parity for Action-Item Tasks, Local NLI Contradiction Radar, and E2EE Vault Sync across Electron Desktop, VS Code, and Streamlit Dashboard, and harden cross-platform CI/release packaging.

**What was built:**
1. Zero-Knowledge E2EE Multi-Device Vault Sync (`scripts/vault_sync.py`, `second-brain sync`, owner-gated `/v1/sync/*` in `scripts/api_product.py`, `MemoryApiClient.sync_*` in `scripts/memory_api.py`): `AES-256-GCM` + `HKDF-SHA256` + `HMAC-SHA256` blind blob IDs, `scrypt` passphrase / 256-bit keyfile modes, `Ed25519` signed heads with peer pinning and anti-rollback epoch checks, Three-Store isolation, append-only JSONL set-union merge, `.history/` fast-forward backups, `0-raw/sync-conflicts/` quarantine, and pluggable `LocalFolderSyncBackend` + DNS-pinned `HttpObjectSyncBackend`.
2. Full 3-Surface UI Parity across Electron Desktop (`desktop/renderer/{index.html,app.js}`), VS Code (`extensions/vscode-second-brain/{package.json,extension.js,lib/markdown-formatters.js}`), and Streamlit Dashboard (`dashboard/app.py`) for Action-Item Tasks (`/v1/notes/tasks` / `extract_action_items`), Local NLI Contradiction Radar (`nli=1` / `enable_nli`), and E2EE Vault Sync (`/v1/sync/*` / `vault_sync`).
3. Hardened `.github/workflows/ci.yml`, `.github/workflows/release.yml`, and `scripts/package_desktop.ps1` (`-SkipSign` + LF checks), verified by Suite `19/19` (`scripts/goal4_sync_and_ui_tests.py`) in `scripts/test_all.py` (`19/19 PASS`).

### Goal 22 — Local-First Browser Web Clipper (MV3), Multi-Vault Workspaces & 20-Suite Gate (`v0.3.0`) — COMPLETE

**Why:** Complete `v0.3.0` Milestones 2 & 3 so owners can clip web pages and verbatim quotes directly into `127.0.0.1` with SHA-256 immutable snapshots and zero cloud relay, switch cleanly across isolated named vaults (`work`, `personal`, `research`) with optional per-vault tokens and strictly opt-in read-only federated recall, and ship unified `v0.3.0` artifacts across every platform.

**What was built:**
1. Local-First Browser Web Clipper (`extensions/browser-clipper/`, `scripts/web_clipper.py`, `scripts/build_clipper.py`, `second-brain clipper`, `/v1/clipper/*`): Manifest V3 Chrome/Edge/Firefox extension restricted strictly to `http://127.0.0.1/*` with zero `.innerHTML`; owner-paired extension origins (`SecondBrain/security/clipper-pairings.json`, `0600`/NTFS DACL) with strict `OPTIONS` CORS preflight allowlisting and write-only SHA-256-hashed `clipper_*` bearer tokens; immutable UTF-8 source snapshots in `brain/0-raw/note-snapshots/clip-*.md` compatible with one-click `verify_note_excerpt` promotion; automatic server-side DLP secret & prompt-injection quarantine to `0-raw/inbox/`.
2. Multi-Vault Workspaces & Opt-In Federated Recall (`scripts/vault_workspaces.py`, `second-brain vault`, `/v1/vaults*`, `MemoryApiClient.*vault*`): Named vault registry (`SecondBrain/security/vaults.json`, `0600`/NTFS DACL) with non-overlapping path validation, atomic active switching synced to `vault-path.txt` and live `MemoryLoopbackHTTPServer` runtime, non-destructive unregister, optional isolated per-vault `vb_*` bearer tokens failing closed (`403`) across vaults, and owner-gated opt-in (`federated_searchable: false` default) read-only federated search.
3. Full 3-Surface UI Parity across Electron Desktop (Sidebar Vault Quick-Switcher + `System` Workspaces & Browser Clipper cards), VS Code (`secondBrain.switchVault`, `secondBrain.federatedSearch`, `secondBrain.pairClipper`), and Streamlit Dashboard (`dashboard/app.py`), aligned `v0.3.0` package manifests (`pyproject.toml`, `desktop/package.json`, `extensions/vscode-second-brain/package.json`, `plugins/`), and Suite `20/20` (`scripts/goal5_clipper_and_workspaces_tests.py`) in `scripts/test_all.py` (`20/20 PASS`).

### Goal 23 — SOTA Retrieval, Auto-Linking Knowledge Graph, Layout/OCR PDF & Cloud Sync (`v0.4.0`) — COMPLETE

**What was built:**
1. Hardware-profiled `qwen2.5-coder:7b` selection, `O(|changed|)` >64-atom Chroma vector upserts (`scripts/rebuild_vectors.py`), local Cross-Encoder reranking (`scripts/cross_encoder.py`), and persistent owner-gated multi-turn chat threads (`scripts/chat_threads.py`).
2. Clause-aligned zero-false-positive NLI contradiction detection + automatic project-scoped `[[wikilink]]` graph linking (`scripts/knowledge_graph.py`).
3. Pure-DOM Readability web clipper + 1-click `SBC-*` pairing codes (`extensions/browser-clipper/`, `scripts/web_clipper.py`) and 2-column/table/OCR PDF extraction (`scripts/doc_extractors.py`).
4. RFC-compliant `AWS4-HMAC-SHA256` S3/R2/B2 SigV4 sync, OS Keychain / Windows DPAPI secret sealing (`scripts/keychain_store.py`), `AutoSyncScheduler`, `sync gc`, and interactive 3-way conflict resolution across all 3 UIs, verified by Suite `21/21` (`scripts/v040_sota_upgrades_tests.py`).

### Goal 24 — Verified ONNX Weights, `qwen3` Hardware Gating, Bi-Temporal Validity & Mobile Inbox Drop (`v0.5.0`) — COMPLETE

**What was built:**
1. SHA-256 + commit-pinned ONNX downloader (`scripts/onnx_models.py`, `second-brain models pull`, `[reranker]` extra) for int8 Cross-Encoder (`Xenova/ms-marco-MiniLM-L-6-v2`) and clause-aligned DeBERTa NLI (`Xenova/nli-deberta-v3-xsmall`).
2. `qwen3` family hardware-tier catalog with live `/api/tags` GGUF RAM-fit gating (`_model_fits_host_memory`) and stateful `<think>...</think>` stripping.
3. Bi-temporal fact validity (`valid_from` / `valid_until`, CAS-preserving `set_atom_temporal_bounds`, point-in-time `as_of`, disjoint-window contradiction suppression, and `expired_validity` drift alerts).
4. Zero-Knowledge `SBID1` Mobile Inbox Drop (`scripts/inbox_drop.py`, `mobile/inbox-drop/` static WebCrypto PWA + iOS Shortcut -> `inbox-drops/<id>.enc` -> quarantined `0-raw/inbox/`), verified by Suite `22/22` (`scripts/v050_sota_complete_tests.py`).

### Goal 25 — Multimodal API/UI Upload, Git Distiller, Bi-Temporal Snapshot Recall, Portable `.sbvault` Backup & `v1.0.0` Distribution — COMPLETE

**What was built:**
1. Owner-gated `POST /v1/ingest/media` (`24 MiB` raw binary & base64 JSON upload, Pillow verification, Whisper timestamps, document extraction, and DLP secret quarantine), wired with clipboard screenshot paste (`Ctrl+V`), `MediaRecorder` voice memos, and media uploads across Electron Quick Capture & Import, VS Code (`secondBrain.ingestMedia`), and Streamlit Dashboard.
2. Git Post-Commit & Coding Session Memory Distiller (`scripts/git_distiller.py`, `second-brain hook`, `/v1/git/*`) with space-safe POSIX single-quoting, worktree/clean-wheel compatibility, user-hook preservation, and pre-write DLP secret redaction across CLI, API/SDK, Desktop `IDE Hub`, VS Code (`secondBrain.installGitHook`, `secondBrain.uninstallGitHook`, `secondBrain.distillGitCommits`), and Dashboard.
3. Bi-temporal `as_of` / `include_expired` + Cross-Encoder `recall_at_snapshot()` and validity-window `diff_snapshots()` wired across MCP, `/v1/snapshots/*` (with non-owner redaction), all 3 UIs, and synced Antigravity + Claude Code `SKILL.md` bundles; plus `watchdog_daemon.py` `--auto-sync` & incremental vector sync (`/v1/daemon/*`, `run_daemon.ps1` / LF `run_daemon.sh`).
4. Portable Three-Store `.sbvault` encrypted (`AES-256-GCM` + `scrypt` + `HKDF-SHA256`) backup, in-memory Merkle/SHA-256 verification, and disaster-recovery restore with automatic FTS5 rebuilds (`scripts/vault_backup.py`, `second-brain backup`, `/v1/vault/backup/*`), plus automated WinGet / Homebrew Cask / Mobile PWA release builders (`scripts/build_distribution_manifests.py`), 300-atom ONNX/bi-temporal SOTA benchmark (`docs/sota-benchmark-results-2026-11-21.json`), and Suite `23/23` (`scripts/v100_final_completion_tests.py`) in `scripts/test_all.py` (`23/23 PASS`).

## Completed product horizon (closed in Goals 17–25, `v1.0.0`)

Goals 0–25 complete the `v1.0.0` verifiable Second Brain platform across Python/CLI, Loopback HTTP API/SDK, 17-tool MCP, 6-host IDE connectors, Electron Desktop, VS Code, Browser Web Clipper MV3, Mobile Inbox Drop PWA, Streamlit Dashboard, WinGet/Homebrew manifests, and the 23-suite unified release gate (`scripts/test_all.py`, `23/23 PASS`).

## Checkpoints

- **Goals 1–2 (Passed):** Curated 30-case dataset validated; grounding enforces
  100% claim precision and 0% trap leakage.
- **Goals 3–5 (Passed):** CAS terminal supersession, loopback/auth boundaries,
  cross-platform installers, and index recovery playbooks verified.
- **Goals 6–7 (Passed):** Project-scoped recall, RRF hybrid fusion, and live
  offline FTS5 degradation verified.
- **Goal 8 (Passed):** Independent adversarial review reconciled and
  `scripts/test_all.py` achieves 100% pass rate.
- **Goals 9–25 (Passed):** Phase 2–5 product ecosystem, local document + multimodal `/v1/ingest/media` ingestion,
  `"research"` leads view, multi-host DLP streaming chat & persistent chat threads, 17-tool MCP, OS-signed
  desktop installers, clean wheel/wheelhouse + WinGet/Homebrew/PWA packaging, 9-system 300-atom SOTA benchmark,
  Three-Store Merkle ledger & bi-temporal Cross-Encoder Time-Travel snapshots, verified ONNX Cross-Encoder & DeBERTa NLI,
  sub-15ms recall, E2EE S3/DPAPI multi-device vault sync, Mobile Inbox Drop PWA, MV3 Browser Web Clipper,
  Multi-Vault Workspaces, Git post-commit distiller, portable `.sbvault` backup/restore, and all UI/E2E/security
  suites verified (`23/23` suites PASS).

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Labels expand faster than owner review | High | Positive labels require current SHA-256 approved atom revisions (`evaluation_dataset.py`) |
| Citation is mistaken for entailment | High | Validate exact claims outside the model and separately audit source byte anchors |
| Interface hardening locks out local users | Medium | Salted `scrypt` CLI password reset (`dashboard_auth.py set-password`) & loopback policy |
| Dependency lock omits native runtime needs | High | `uv.lock` + `setup.ps1`/`setup.sh` C++ ABI checks (`vcruntime140.dll`/`libstdc++`) |
| Dense embedder offline drops user queries | Medium | Automatic dynamic fallback to SQLite FTS5 with explicit `[WARNING]` banner |
| Legacy history leaked on future remote push | High | Audited `bffca9e` and prepared clean `release-code-only` orphan branch |
