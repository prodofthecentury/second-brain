# Task Queue: Dependable Local Brain Pilot

This queue is reconciled with current source, `docs/system-sweep-2026-10-27.md`,
`docs/system-sweep-2026-11-15.md`, `docs/independent-review-2026-11-15.md`, and
`docs/phase2-architecture-and-sweep-2026-11-15.md`.
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

### Goal 9 — Hardware-tiered capability profiles — COMPLETE

- [x] Implement deterministic OS/CPU/RAM/VRAM/C++ ABI inspection and loopback-only
  Ollama probing (`scripts/hardware_profile.py`) across `tier_0_lexical_only` to
  `tier_3_gpu_accelerated` with 30s TTL caching and zero-proxy/zero-redirect
  SSRF protection.
- [x] Expose capability tiers, retrieval/answer latency SLOs, and recommended
  models in `second-brain profile`, `second-brain doctor`, `setup.ps1`,
  `setup.sh`, Dashboard System Health, and `GET /v1/profile`.
- **Depends on:** Goal 5.
- **Details:** `tasks/plan.md` Goal 9.

### Goal 10 — Quarantined inbox promotion and multi-atom Refinery — COMPLETE

- [x] Implement deterministic risk scanning, SHA-256 CAS owner promotion, and
  CAS rejection for `brain/0-raw/inbox/` (`scripts/inbox_workflow.py`) with
  frontmatter/HTML-comment neutralization, inline/angle/reference/`<img>` beacon
  defanging, collision-safe archives, and origin anti-laundering
  (`scripts/memory_format.py`).
- [x] Expose Quarantined Inbox triage in Dashboard (`dashboard/app.py`) and
  `GET /v1/inbox/queue`, and enforce verbatim-quote multi-atom candidate
  extraction in `scripts/ollama_refinery.py`.
- **Depends on:** Goals 3–4.
- **Details:** `tasks/plan.md` Goal 10.

### Goal 11 — Per-host online and custom AI adapters — COMPLETE

- [x] Implement default-deny per-host sharing rules (`host-sharing-policy.json`
  with DACL/`0600` and symlink guards), multi-project `all(...)` scope
  containment, outbound credential blocking, wire-level PII placeholder
  scrubbing with strict local claim rehydration, DNS-pinned `https://...:443`
  transport, and outbound audit logging (`scripts/host_adapters.py`).
- [x] Route all local (`ollama_local`, `owner_custom_local`) and online
  (`openai_online`, `anthropic_online`, `gemini_online`) draft generations
  through `scripts/answer_policy.py` across CLI, Dashboard, and Loopback API.
- **Depends on:** Goals 2, 4, and 6.
- **Details:** `tasks/plan.md` Goal 11.

### Goal 12 — Cognitive Core telemetry, fitness, and experience episodes — COMPLETE

- [x] Implement disposable SQLite WAL telemetry (`brain/.sidecar/telemetry.sqlite3`)
  for retrieval, candidate bottleneck (`peek_lexical_candidate_atom_ids`),
  citation, and feedback metrics with zero canonical atom hash churn
  (`scripts/cognitive_core.py`).
- [x] Compute memory fitness and procedural skill maturity (`procedural_draft` ->
  `verified_procedure` -> `skill_candidate`), and persist create-only episode
  logs in `brain/4-experiences/` with optional quarantined procedural proposals
  in `brain/0-raw/inbox/`.
- **Depends on:** Goals 2–3 and 10.
- **Details:** `tasks/plan.md` Goal 12.

### Goal 13 — Loopback Memory API, SDK, and Phase 2 security audit — COMPLETE

- [x] Implement multi-threaded `127.0.0.1` HTTP JSON service (`second-brain serve`),
  typed `MemoryApiClient` SDK, and IDE/host adapter templates
  (`second-brain adapter-config`) in `scripts/memory_api.py` with 256-bit Bearer
  auth, `Host`/`Origin`/`Sec-Fetch-Site` guards, socket timeouts, and valid-token
  lockout immunity.
- [x] Complete isolated `security-auditor` adversarial review
  (`b8248c56-e4ba-423f-85d9-1fda32190901`), remediate all 12 findings, add
  regression coverage in `scripts/phase2_safety_tests.py`, and verify `7/7`
  suites pass (`100% PASS`) in `scripts/test_all.py`.
- **Depends on:** Goals 8–12.
- **Details:** `tasks/plan.md` Goal 13.

### Goal 14 — Context/Fitness Ranking, Skill Playbooks & Grounded Executive Planner (Phase 3A) — COMPLETE

- [x] Implement fitness-aware ranking, validated `best_bm25` normalization, and
  content-hashed bounded working-context assembly (`scripts/recall.py`), backed
  by durable `brain/0-raw/archive/atom-corrections.jsonl` (`scripts/cognitive_core.py`).
- [x] Standardize owner-gated CAS Skill Playbooks under `brain/5-playbooks/`
  (`scripts/skill_playbooks.py`, `scripts/config.py`) with SHA-256 decision log
  verification, `.history/` backups, whole-Claim step grounding, and per-atom
  project scope gates.
- [x] Implement grounded Executive Planner (`scripts/executive_planner.py`),
  `second-brain playbooks` / `plan`, `GET /v1/playbooks`, `POST /v1/plan`, and
  Dashboard Playbooks & Planner tabs (`dashboard/app.py`).
- [x] Complete independent `security-auditor` review (`dda00b6d-652b-41d6-adb9-2fc45492e3cf`)
  and lock down `H1`, `H2`, `M1`–`M4`, `L1`–`L7` in `scripts/phase3_cognitive_tests.py`
  (13 tests passing).
- **Depends on:** Goals 12–13.
- **Details:** `tasks/plan.md` Goal 14.

### Goal 15 — Scale Engine, Zero-Friction Notes, Excerpt Verification & Bulk Importer (Phase 4) — COMPLETE

- [x] Implement stat-cached `os.scandir()` revision scans (`scripts/index_manifest.py`),
  incremental FTS5 upsert/sync/fast browse (`scripts/lexical_store.py`), tail-cached
  decision logs (`scripts/atom_mutator.py`), SHA-256 cached source normalization
  (`scripts/evidence.py`), Windows-safe Chroma activation retries
  (`scripts/rebuild_vectors.py`), and `scripts/scale_benchmark.py`.
- [x] Build zero-friction owner notes (`scripts/notes_store.py`, `brain/notes/`,
  `brain/.sidecar/notes.sqlite3`) with CAS updates, quarantine for risky captures,
  and one-click `verify_note_excerpt()` immutable snapshotting under
  `brain/0-raw/note-snapshots/`.
- [x] Implement idempotent Markdown/Obsidian bulk importer (`scripts/bulk_import.py`,
  `second-brain import`).
- **Depends on:** Goals 5, 7, 13.
- **Details:** `tasks/plan.md` Goal 15.

### Goal 16 — Streaming Chat, Dual-Token Product API & IDE Connectors (Phase 4) — COMPLETE

- [x] Build `instant` / `verified` / `stream` chat (`scripts/streaming_chat.py`,
  `second-brain chat`, `POST /v1/chat/stream`, `POST /v1/chat/warmup`).
- [x] Expose dual-token Loopback Product API (`scripts/api_product.py`, `scripts/memory_api.py`)
  separating `owner_authorized` mutations from agent read/quarantined `/v1/remember`.
- [x] Ship one-click IDE/host connector (`scripts/ide_connector.py`,
  `second-brain connect`), plugin bundles (`plugins/antigravity/second-brain/`,
  `plugins/claude-code/second-brain/`), Electron desktop app (`desktop/`), VS Code
  extension (`extensions/vscode-second-brain/`), Authenticode signing script
  (`scripts/sign_release.ps1`), custom planner schema support (`scripts/host_adapters.py`),
  MCP notes/remember tools with `trusted_stdio_session()`, and 32 tests in
  `scripts/phase4_product_tests.py` (including Phase 4 audit remediations
  `P4-HIGH-01`..`P4-MED-04` and Phase 5 follow-ups) integrated into the 11-suite
  `scripts/test_all.py` release gate (`11/11 PASS`).
- **Depends on:** Goals 13–15.
- **Details:** `tasks/plan.md` Goal 16.

### Goal 17 — OS-Signed Desktop Installers, SOTA Benchmark & Phase 5 Verifiable Vault Suite (`v0.2.0`) — COMPLETE

- [x] Add cross-platform OS-signed desktop installer targets (`nsis`, `msi`, `dmg`/`zip`, `AppImage`/`deb`) in `desktop/package.json`, unified `.exe`/`.msi`/`.whl`/`.vsix` build + signing pipeline (`scripts/package_desktop.ps1`, `scripts/package_desktop.sh`, `scripts/sign_release.ps1`), automatic first-run `.venv` bootstrap (`ensureBackendBootstrapped` + bundled `setup.ps1`/`setup.sh`), 8-tab Electron UI (`Playbooks & Plan` + `Vault Proof & Radar`), and live tray lifecycle controls (`Start`/`Restart`/`Stop Memory API`, `Open Vault Folder`, `Launch at Login`) in `desktop/main.js`.
- [x] Implement comparative SOTA benchmark harness (`scripts/sota_benchmark.py`, `second-brain sota-benchmark`) comparing Second Brain against external RAG/memory baselines on multi-hundred-atom owner-reviewed corpora with declared hardware profiles.
- [x] Implement Cryptographic Vault Merkle Proof & Audit Ledger (`scripts/vault_ledger.py`, `second-brain ledger`, `/v1/ledger/*`), Contradiction & Drift Radar + Knowledge Graph (`scripts/knowledge_graph.py`, `second-brain graph`, `/v1/graph/*`), and Time-Travel Vault Snapshots, Semantic Diff & Point-in-Time Recall (`scripts/time_travel.py`, `second-brain snapshot`, `/v1/snapshots/*`), verified by `scripts/phase5_god_features_tests.py` in the 12-suite `scripts/test_all.py` release gate (`12/12 PASS`).

### Goal 18 — Core Runtime Parity, Three-Store Durability, Security Redaction & Sub-15ms Recall — COMPLETE

- [x] Fix `second-brain mcp --generate-token` (`save_bearer_token`), persist/discover `SecondBrain/security/api-port` across Desktop/CLI/SDK/VS Code, unify `SecondBrain/security/vault-path.txt` across CLI/Dashboard/MCP/Desktop, and align CLI flags (`capture --tags`, `notes add/verify`, `import --overwrite-modified`) with `README.md`.
- [x] Extract `## Evidence` and `epistemic_contract` in `parse_atom_record()`, propagate `project:*` / `context:*` tags into `contexts:` in `verify_note_excerpt()`, unify multi-project `project_tags_match()`, tighten non-owner `/v1/snapshots/*` & `/v1/audit/sources*` redaction/counts, persist durable `actor`/`note` feedback in `atom-corrections.jsonl` (excluding `ret_hits` and `supersession` penalties), gate `qwen3.6:latest` by RAM/VRAM, fix `doctor.py` `errors` key, log `outcome="denied"` on unauthorized MCP tool calls, and enforce loopback no-proxy/no-redirect probes in `setup.ps1` / `setup.sh`.
- [x] Migrate `vault-ledger.jsonl` and content-addressed `schema_version: 3` snapshots to durable `brain/0-raw/archive/` (with `.sidecar/` fallback), generalize atom+playbook Merkle proofs (`prove_leaf_membership`), upgrade `time_travel.py` to Porter stemming, implement spec-compliant YAML/JSON frontmatter parsing, support single-term & calibrated semantic recall (`Recall@5 = 1.000`, `0/10` traps, sub-15ms P95), bound `MemoryLoopbackHTTPServer` worker/SSE threads, serve fast FTS5-backed `GET /v1/atoms`, and relocate operator runbooks to `docs/runbooks/`.

### Goal 19 — Document Ingestion, Chunked Refinery, Incremental Vectors, Research View, Multi-Host DLP Chat, Semantic NLI Radar & 17-Tool MCP — COMPLETE

- [x] Implement local document extraction and import (`scripts/doc_extractors.py`, `scripts/ingest_multimodal.py`, `scripts/bulk_import.py`) for `.pdf`, `.docx`, `.epub`, `.html`, `.csv`, and `.tsv` with immutable binary archival, `ARCHIVE_MANIFEST` SHA-256 lineage, `[page N]` coordinate spans, `[MM:SS.mmm -> MM:SS.mmm]` Whisper timestamps, and fail-closed Pillow validation.
- [x] Add context-window-safe Refinery chunking with verbatim byte-offset preservation (`scripts/ollama_refinery.py`), incremental vector carry-forward (`scripts/rebuild_vectors.py`), batched atomic `JsonVectorCollection` writes (`scripts/vector_store.py`), hardware-gated watchdog daemon with debounced FTS5 sync (`scripts/watchdog_daemon.py`), honest Editor deferral & project scope partitioning (`scripts/ollama_editor.py`), 8-field `ExperienceRecord` spec (`scripts/cognitive_core.py`), and structured action-item extraction (`scripts/notes_store.py`).
- [x] Implement `"research"` answer mode with deterministic `[UNVERIFIED LEADS]` (`scripts/answer_policy.py`), multi-host DLP streaming chat with DNS pinning and PII redaction/rehydration (`scripts/streaming_chat.py`, `scripts/host_adapters.py`), semantic/NLI Contradiction & Drift Radar (`scripts/knowledge_graph.py`), 17-tool MCP server (`scripts/mcp_server.py`), 6-host rule-installed IDE connectors (`scripts/ide_connector.py`), and 9-system comparative SOTA benchmark (`scripts/sota_benchmark.py`), verified by `scripts/goal2_completion_tests.py`.

### Goal 20 — Full Desktop/VS Code/Dashboard UI Parity, Clean Wheel Namespace, Offline Wheelhouse & 18-Suite Release Gate — COMPLETE

- [x] Deliver full Phase 4/5 and Goal 2 UI parity across Electron Desktop (`desktop/`), VS Code extension (`extensions/vscode-second-brain/`), and Streamlit Dashboard (`dashboard/app.py`), including `"research"` mode & `[UNVERIFIED LEADS]`, multi-host streaming chat, document import, Merkle proof verification, Contradiction Radar, and Time-Travel snapshots.
- [x] Package clean `second_brain` Python wheel namespace (`second_brain/`), offline wheelhouse builder (`scripts/build_wheelhouse.py`), pure-Python `.vsix` builder (`scripts/build_vsix.py`), multi-format desktop icon generator (`scripts/make_icons.py`), and inner+outer Authenticode signing (`desktop/build/sign.js`, `scripts/sign_release.ps1`).
- [x] Add automated behavioral test suites for Electron Desktop DOM/XSS/graph/pagination (`scripts/run_desktop_tests.py`), VS Code extension SSE/singleton/governance (`scripts/run_vscode_tests.py`), Streamlit AppTest dashboard (`scripts/dashboard_apptest_tests.py`), backend contracts (`scripts/goal3_backend_contract_tests.py`), and clean wheel/VSIX/wheelhouse packaging (`scripts/wheel_packaging_tests.py`), achieving `18/18 PASS` (`100% PASS`) in `scripts/test_all.py`.

### Goal 21 — Zero-Knowledge E2EE Multi-Device Vault Sync, Full 3-Surface UI Parity & 19-Suite Gate (`v0.3.0`) — COMPLETE

- [x] Implement Zero-Knowledge E2EE Multi-Device Vault Sync (`scripts/vault_sync.py`, `second-brain sync`, owner-gated `/v1/sync/*` routes in `scripts/api_product.py`, `MemoryApiClient.sync_*` in `scripts/memory_api.py`, `cryptography` promoted in `pyproject.toml`/`uv.lock`) with `AES-256-GCM`, `HKDF-SHA256`, `HMAC-SHA256` blind blob IDs, `Ed25519` signed heads, anti-rollback enforcement, Three-Store isolation, append-only JSONL set-union merge, `.history/` backups, `0-raw/sync-conflicts/` quarantine, and `local_folder` / DNS-pinned `http_object` backends.
- [x] Complete 3-surface UI parity for Action-Item Tasks, Local NLI Contradiction Radar (`nli=1`), and E2EE Vault Sync across Electron Desktop (`desktop/renderer/`), VS Code (`extensions/vscode-second-brain/`), and Streamlit Dashboard (`dashboard/app.py`).
- [x] Harden `.github/workflows/ci.yml`, `.github/workflows/release.yml`, and `scripts/package_desktop.ps1` (`-SkipSign` support + LF script checks), and add Suite `19/19` (`scripts/goal4_sync_and_ui_tests.py`) achieving `19/19 PASS` (`100% PASS`) in `scripts/test_all.py`.

### Goal 22 — Local-First Browser Web Clipper (MV3), Multi-Vault Workspaces & 20-Suite Gate (`v0.3.0`) — COMPLETE

- [x] Implement Local-First Browser Web Clipper (`extensions/browser-clipper/`, `scripts/web_clipper.py`, `scripts/build_clipper.py`, `second-brain clipper`, `/v1/clipper/*`) with strict `http://127.0.0.1/*` MV3 permissions, zero `.innerHTML`, SHA-256-hashed write-only `clipper_*` bearer tokens bound to owner-paired extension origins, immutable `brain/0-raw/note-snapshots/clip-*.md` snapshots compatible with `verify_note_excerpt`, and server-side DLP/prompt-injection quarantine.
- [x] Implement Multi-Vault Workspaces & Opt-In Federated Recall (`scripts/vault_workspaces.py`, `second-brain vault`, `/v1/vaults*`, `MemoryApiClient.*vault*`) with `SecondBrain/security/vaults.json`, non-overlapping directory checks, atomic `vault-path.txt` + live API hot-switching, optional isolated per-vault `vb_*` tokens, non-destructive unregister, and strictly opt-in read-only federated search.
- [x] Complete 3-surface UI parity (Electron Desktop sidebar switcher + System cards, VS Code commands, Streamlit Dashboard), align `v0.3.0` package manifests across Python/Electron/VS Code/plugins, and add Suite `20/20` (`scripts/goal5_clipper_and_workspaces_tests.py`) achieving `20/20 PASS` (`100% PASS`) in `scripts/test_all.py`.

### Goal 23 — SOTA Retrieval, Auto-Linking Knowledge Graph, Layout/OCR PDF & Cloud Sync (`v0.4.0`) — COMPLETE

- [x] Auto-wire `qwen2.5-coder:7b` on Tier 2+, `O(|changed|)` >64-atom Chroma vector upserts, local Cross-Encoder reranker (`scripts/cross_encoder.py`), and persistent multi-turn chat threads (`scripts/chat_threads.py`).
- [x] Ship clause-aligned 0-FP NLI contradiction detection + automatic project-scoped `[[wikilink]]` graph linking (`scripts/knowledge_graph.py`), Readability MV3 clipper + 1-click `SBC-*` pairing codes, 2-column/table/OCR PDF extraction, and S3 SigV4 + DPAPI/Keychain + `AutoSyncScheduler` + 3-way conflict resolution (`21/21 PASS` via `scripts/v040_sota_upgrades_tests.py`).

### Goal 24 — Verified ONNX Weights, `qwen3` Hardware Gating, Bi-Temporal Validity & Mobile Inbox Drop (`v0.5.0`) — COMPLETE

- [x] Add SHA-256 + commit-pinned ONNX downloader (`scripts/onnx_models.py`, `second-brain models pull`, `[reranker]` extra), `qwen3` GGUF RAM-fit gating + `<think>` stripping, bi-temporal `valid_from`/`valid_until`/`as_of` validity across recall/MCP/API/graph, and Zero-Knowledge `SBID1` Mobile Inbox Drop PWA + iOS Shortcut (`scripts/inbox_drop.py`, `mobile/inbox-drop/`), achieving `22/22 PASS` (`scripts/v050_sota_complete_tests.py`).

### Goal 25 — Multimodal API/UI Upload, Git Distiller, Bi-Temporal Snapshot Recall, Portable `.sbvault` Backup & `v1.0.0` Distribution — COMPLETE

- [x] Wire owner-gated `POST /v1/ingest/media` (`24 MiB` binary & base64 JSON upload, Pillow gate, Whisper timestamps, DLP quarantine) + clipboard screenshot paste, voice memos, and media uploads across Desktop, VS Code (`secondBrain.ingestMedia`), and Dashboard.
- [x] Harden and wire `scripts/git_distiller.py` (`second-brain hook`, `/v1/git/*`) across CLI, API/SDK, Desktop `IDE Hub`, VS Code (`secondBrain.{installGitHook,uninstallGitHook,distillGitCommits}`), and Dashboard; complete bi-temporal + Cross-Encoder `recall_at_snapshot()` & `diff_snapshots()` redaction + `SKILL.md` parity + live Antigravity connector sync; and expose `watchdog_daemon.py` `--auto-sync` & incremental vector sync (`/v1/daemon/*`, `run_daemon.ps1`, LF `run_daemon.sh`).
- [x] Implement portable Three-Store `AES-256-GCM` `.sbvault` backup, in-memory Merkle verification, and FTS5-rebuilding restore (`scripts/vault_backup.py`, `second-brain backup`, `/v1/vault/backup/*`), automated WinGet / Homebrew Cask / Mobile PWA release builders (`scripts/build_distribution_manifests.py`), 300-atom ONNX/bi-temporal SOTA benchmark (`docs/sota-benchmark-results-2026-11-21.json`), unified `1.0.0` manifests, adversarial security/code audits, and Suite `23/23` (`scripts/v100_final_completion_tests.py`) achieving `23/23 PASS` (`100% PASS`) in `scripts/test_all.py`.

### Goal 26 — `v1.0.0` Partial-Gap Closure: Git Scrub, Validated Installers, Sub-15ms ONNX Latency, 90-Case Live Third-Party Benchmarks, Live Operational Vault & Bidirectional Mobile Recall — COMPLETE

- [x] **Git & Release Hygiene:** Scrubbed legacy 18-file vault commit `bffca9e` from `master` root history (`98ce98a` code-only root), synchronized `release-code-only` and annotated tag `v1.0.0` with `master`, and remediated 11/11 findings from the independent adversarial `CODEX_CLI_GOAL.md` review (`docs/independent-review-2026-11-22-v1.0.0.md`).
- [x] **Distribution & Installers:** Fixed semver boundary filtering, BOM handling, SHA-256 verification, WinGet (`winget validate` PASS), and Homebrew `Formula` generation (concrete URL, real `sha256`, zero `:no_check`) in `scripts/build_distribution_manifests.py`, rebuilding all `1.0.0` Windows `.exe`/`.msi`, `.whl`, `.tar.gz`, `.vsix`, clipper, and mobile bundles (`dist/SHA256SUMS.txt`).
- [x] **Local Generation Runtime & Latency:** Verified live Ollama (`nomic-embed-text:latest`, `qwen2.5-coder:7b`, `llama3.2:3b`, `llava:latest`) with RAM-fit gating (`23 GB` `qwen3.6:latest` gated on `tier_2_cpu_balanced`), eliminated redundant Windows `CreateFileW` reparse syscalls to bring 300-atom ONNX Cross-Encoder total P95 to `13.59 ms` (`< 15.0 ms` Tier-0 target), and verified warm CPU answer P95 inside the `35.0 s` Tier-2 SLO.
- [x] **Benchmarking:** Expanded `docs/retrieval-evaluation-proposed.json` (`v3.0.0`) from 30 to **90 human-curated cases** (`25` answerable, `15` preference, `25` exploratory, `25` traps across `30/30/30` splits; `1.000` trusted recall, `0` trap FPs, `100%` answer grounding), added live third-party comparisons (`mem0ai`, `faiss-cpu`, `chromadb`, `rank-bm25`) in `scripts/sota_benchmark.py` and `scripts/live_third_party_benchmark.py`, and published `docs/benchmark-report-v1.0.0.md`.
- [x] **Live Vault & Operational Setup:** Auto-linked all `75` owner-approved atoms (`75` nodes, `141` edges, `0` orphans), recorded real episodes in `brain/4-experiences/`, installed `.git/hooks/post-commit`, and provisioned E2EE Sync (DPAPI), Mobile Drop/Recall, Browser Clipper, and Multi-Vault Workspaces (`default`, `work`, `research`) in `%LOCALAPPDATA%\SecondBrain\security`.
- [x] **Mobile Footprint & 26-Suite Gate:** Extended `mobile/inbox-drop/` + `scripts/inbox_drop.py` with opt-in `SBMR1` (`HKDF-SHA256 + AES-256-GCM`) encrypted vault recall, client-side BM25 search, `project:` filtering, and 1-tap review notes alongside the full on-device Mobile Vault Client (`mobile/vault-client/`), expanding `scripts/test_all.py` to **26/26 suites (`100% PASS`)**.

## Completed product horizon & Former Partial Section (100% Complete in Goals 17–26, `v1.0.0`)

- [x] **OS-Signed Desktop Installers & Package Managers:** Native code-signed NSIS `.exe` / `.msi` / DMG / AppImage / deb targets, validated WinGet (`SecondBrain.SecondBrain.yaml`), and concrete Homebrew `Formula` (`second-brain.rb`) verified against `dist/SHA256SUMS.txt`.
- [x] **Human-Curated 90-Case & Live Third-Party SOTA Benchmarking:** `docs/retrieval-evaluation-proposed.json` (`90` curated cases) + `scripts/live_third_party_benchmark.py` & `scripts/sota_benchmark.py` benchmarking Second Brain head-to-head against live `Mem0` (`mem0ai`), `FAISS` (`faiss-cpu`), `ChromaDB`, `rank-bm25`, and `SQLite FTS5` (`docs/benchmark-report-v1.0.0.md`).
- [x] **Entire Former Partial Section Closed (`6/6` Complete):** Git & Release Hygiene, Distribution & Installers, Local Generation Runtime & Latency, Benchmarking, Live Vault & Operational Setup, and Bidirectional Mobile Footprint all verified green (`docs/independent-review-2026-11-22-v1.0.0.md`).
