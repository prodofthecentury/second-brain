# Phase 3A & Phase 4 Architecture, Security Sweep & Scale Verification (2026-11-15)

## 1. Executive Summary

Phase 3A and Phase 4 extend Second Brain from the Phase 2 Host-Independent Loopback API & Cognitive Telemetry foundation into full **Procedural Skill Playbooks**, **Grounded Executive Planning**, **Corpus-Scale FTS5 Indexing**, **Zero-Friction Owner Notes**, **Streaming / Instant / Verified Chat**, and **One-Click IDE & Agent Connectors**, while preserving all **5 Non-Negotiable System Invariants**:

1. **Durable Storage Invariance:** Owner-verified knowledge lives exclusively in canonical Markdown atoms (`brain/2-atoms/*.md`), owner-gated Playbooks (`brain/5-playbooks/*.md`), immutable evidence artifacts (`brain/0-raw/*`, `brain/sources/*`), and create-only episodes (`brain/4-experiences/*.md`). Unverified working notes live separately in `brain/notes/*.md` with immutable snapshots taken on verification under `brain/0-raw/note-snapshots/`. All SQLite (`lexical.sqlite3`, `notes.sqlite3`, `telemetry.sqlite3`) and Chroma (`.chroma/`) sidecars under `brain/.sidecar/` remain rebuildable projections.
2. **Deterministic Compare-and-Swap (CAS):** Canonical atoms (`scripts/atom_mutator.py`) and Skill Playbooks (`scripts/skill_playbooks.py`) require `writer.lock` and explicit `expected_sha256` / `expected_playbook_sha256` preconditions alongside append-only SHA-256 decision logs (`atom-decisions.jsonl`, `playbook-decisions.jsonl`).
3. **Cryptographic Source Anchoring:** Every approved atom requires an exact byte-offset + raw-byte SHA-256 anchor (`scripts/audit_sources.py`). Promoting a note excerpt (`verify_note_excerpt`) snapshots exact bytes into `brain/0-raw/note-snapshots/` before anchoring and approval.
4. **Out-of-Model Epistemic & Procedural Grounding:** Standard answers (`scripts/answer_policy.py`), Playbook step grounding (`_claim_grounding_units`), and Executive Plans (`scripts/executive_planner.py`) validate claims, steps, and scopes outside the model. `model_assisted` planning expands selected `PB_*` Playbooks into complete authored sequences so models cannot drop or reorder steps.
5. **Fail-Closed Security & Dual-Token Separation:** Owner-privileged mutations require the desktop/CLI owner credential (`owner_authorized` in `scripts/api_product.py`). Unverified captures via `add_to_inbox`, `/v1/inbox`, or risky `remember` / `/v1/remember` payloads stay quarantined in `brain/0-raw/inbox/`.

---

## 2. Phase 3A Engineering Tracks (Cognitive Core Layers 4–5)

### Track A: Context-Profile & Fitness-Aware Ranking + Bounded Assembler (`scripts/recall.py`, `scripts/cognitive_core.py`)
- **Fitness & Context-Profile Ranking:** `scripts/recall.py` supports context profiles (`active_context_profile`) and opt-in utility ranking (`enable_fitness_boost`) driven by `grounded_citations`, `positive_feedback`, and `corrections` while strictly excluding raw `retrieval_hits` from `utility` (`+ (grounded_citations * 2.0) + (positive_feedback * 1.5) - (corrections * 3.0)`).
- **Validated Normalization & Bounded Working Context:** BM25 normalization (`best_bm25`) runs exclusively over verified candidates passing `verified_atom_content`, `candidate_has_query_overlap`, and `result_from_candidate`. `assemble_bounded_working_context` deduplicates cleanly by hashing `item.content` directly within configured working-context budgets.
- **Durable Canonical Correction Log:** `scripts/cognitive_core.py` records negative feedback (`record_atom_feedback(..., helpful=False)`) to durable `brain/0-raw/archive/atom-corrections.jsonl` (`_append_canonical_correction` / `count_canonical_corrections`), ensuring demotions and Playbook disqualifications persist even if disposable `telemetry.sqlite3` is deleted or corrupted.

### Track B: Owner-Gated Skill Playbooks (`scripts/skill_playbooks.py`, `brain/5-playbooks/`)
- **Canonical Location:** Playbooks live exclusively under `PLAYBOOKS_DIR = brain/5-playbooks` (`scripts/config.py`) with revision snapshots preserved in `brain/5-playbooks/.history/`.
- **Maturity & Promotion Gate:**
  - `inspect_skill_candidates()` evaluates approved `procedural` atoms against citation maturity and `_effective_corrections()` (combining SQLite telemetry and canonical `atom-corrections.jsonl`, returning `None` and failing closed on SQLite errors).
  - `promote_skill_playbook()` acquires `writer.lock`, validates a human actor (`_validate_human_actor`), requires `expected_playbook_sha256` CAS matching on re-promotion, rejects backing atoms with differing project scopes (or mixing scoped and unscoped atoms) unless `allow_multi_project=True` / `--allow-multi-project` is explicitly granted, grounds each step against whole Claim sentences/list items (`_claim_grounding_units` over `_raw_section_block` text), snapshots any replaced revision (`_backup_playbook_revision` with `O_CREAT | O_EXCL` and reparse checks), and appends a SHA-256 record to `brain/0-raw/archive/playbook-decisions.jsonl`.
- **Fail-Closed Integrity Verification & Deprecation:**
  - `verify_playbook_integrity()` checks `_is_reparse_or_link(playbooks_dir)`, requires `playbook_id == filename stem`, hashes exact file bytes once, verifies exact SHA-256 parity with the latest `promote_playbook` record in `playbook-decisions.jsonl`, and re-validates every backing atom's approval, source anchor, and zero-correction status.
  - `deprecate_skill_playbook()` edits strictly inside YAML frontmatter (`deprecated_at`, `deprecated_by`) with re-parse validation and audit logging.
  - `playbook_visible_in_scope()` enforces per-backing-atom containment across requested `project:<id>` scopes across CLI, planner, and `/v1/playbooks?project=`.
- **Surfaces:** `second-brain playbooks {candidates,promote,list,deprecate}`, `GET /v1/playbooks`, and Dashboard `📘 Skill Playbooks` tab.

### Track C: Grounded Executive Planner (`scripts/executive_planner.py`)
- **Grounded Plan Assembly:** `assemble_executive_plan(ExecutivePlanRequest, ...)` retrieves verified active Playbooks matching the goal within project scope (`_playbook_matches_goal` via `playbook_visible_in_scope`) and approved, source-anchored Atoms (`_verified_atom_citation`), reports project-scoped unapproved pending counts (`_count_unapproved_candidates` filtered via `project_tags_match`), and saves structured briefings (`_save_plan_briefing`).
- **Host Scope Containment & Model-Assisted Safeguards:**
  - `_playbook_allowed_for_host` / `_contexts_allowed_for_host` check every backing atom's contexts individually against `HostSharingRule` before sharing Playbooks with any external/online host.
  - `model_assisted` planning validates drafts against `EXECUTIVE_PLAN_DRAFT_SCHEMA` (strict JSON Schema without `maxLength`) and expands selected `PB_*` identifiers into complete authored Playbooks in original step order.
- **Surfaces:** `second-brain plan`, `POST /v1/plan`, and Dashboard `🧭 Executive Planner` tab.

---

## 3. Phase 3A Independent Security Audit Reconciliation

An isolated read-only `security-auditor` sweep (`dda00b6d-652b-41d6-adb9-2fc45492e3cf`) reviewed Phase 3A modules (`recall.py`, `cognitive_core.py`, `skill_playbooks.py`, `executive_planner.py`, `memory_api.py`). Every finding was remediated and verified in `scripts/phase3_cognitive_tests.py`:

| ID | Severity | Finding Summary | Remediation & Regression Verification |
| --- | --- | --- | --- |
| **H1** | High | Out-of-band Playbook edits or ID/filename mismatches could spoof active verified Playbooks if only backing atoms were checked. | Enforced `playbook_id == filename stem` and single-read byte SHA-256 verification against latest `promote_playbook` record in `0-raw/archive/playbook-decisions.jsonl` (`verify_playbook_integrity`, `test_h1_out_of_band_playbook_edits_fail_closed`). |
| **H2** | High | Multi-project Playbook promotion or merged-tag filtering could leak single-project backing context into wrong scopes. | Gated differing/mixed backing-atom project scopes behind explicit `allow_multi_project`, enforced per-backing-atom `playbook_visible_in_scope` in planner & `/v1/playbooks?project=`, and added CAS `expected_playbook_sha256` on re-promotion (`test_h2_m1_mixed_scope_playbooks_rejected_and_per_atom_scope_enforced`). |
| **M1** | Medium | Planner host-sharing check evaluated merged Playbook contexts rather than checking each backing atom individually. | Updated `_playbook_allowed_for_host` / `_contexts_allowed_for_host` to require every backing atom context set to comply with `HostSharingRule` (`test_h2_m1_mixed_scope_playbooks_rejected_and_per_atom_scope_enforced`). |
| **M2** | Medium | Negative feedback / corrections stored solely in disposable `telemetry.sqlite3` vanished on DB deletion/corruption, allowing disqualified Playbooks to reactivate. | Persisted corrections durably to `0-raw/archive/atom-corrections.jsonl`, returned `None` on SQLite read errors in `_load_telemetry_counts`, and failed closed in `_effective_corrections` (`test_m2_corrections_are_durable_and_fail_closed`). |
| **M3** | Medium | Substring Playbook step matching against collapsed `Claim` + `Evidence` text allowed negation stripping or grounding from `Evidence`. | Restricted grounding to `_claim_grounding_units` (whole Claim, bullet/numbered Claim items, full sentences) over raw newline-preserving `## Claim` text (`_raw_section_block`) (`test_m3_substring_steps_cannot_strip_negation_or_use_evidence`). |
| **M4** | Medium | `model_assisted` planner allowed models to select arbitrary step subsets or reorder Playbook steps. | Expanded `model_assisted` `PB_*` selections into full authored Playbooks in original step order (`test_m4_model_cannot_drop_or_reorder_playbook_steps`). |
| **L1** | Low | `best_bm25` normalization considered raw unvalidated candidates before overlap/verification filtering. | Computed `best_bm25` strictly over candidates passing `verified_atom_content`, `candidate_has_query_overlap`, and `result_from_candidate` (`ContextFitnessAndBoundedWorkingContextTests`). |
| **L2** | Low | `retrieval_hits` inflated atom `utility` scores purely from repeated impressions. | Excluded `retrieval_hits` from `utility` (`grounded_citations * 2.0 + positive_feedback * 1.5 - corrections * 3.0`) (`ContextFitnessAndBoundedWorkingContextTests`). |
| **L3** | Low | Playbook `.history/` revision backups lacked exclusive creation and symlink/reparse validation. | Hardened `_backup_playbook_revision` with `O_CREAT | O_EXCL` and `_is_reparse_or_link` guards (`scripts/skill_playbooks.py`). |
| **L4** | Low | Playbook reads and deprecation did not check `playbooks_dir` itself for reparse/symlink substitution. | Added `_is_reparse_or_link(playbooks_dir)` checks on Playbook reads and deprecation (`scripts/skill_playbooks.py`). |
| **L5** | Low | Playbook deprecation string substitution could alter body text if metadata keys appeared outside frontmatter. | Restricted deprecation edits (`deprecated_at`, `deprecated_by`) strictly to YAML frontmatter with re-parse validation (`test_l5_deprecation_edits_only_frontmatter_and_l7_pending_count_is_scoped`). |
| **L6** | Low | Bounded working-context deduplication hashed normalized snippets instead of canonical `item.content`. | Hashed `item.content` directly inside `assemble_bounded_working_context` (`scripts/recall.py`). |
| **L7** | Low | Executive Planner `_count_unapproved_candidates` counted pending items globally across unrelated projects. | Scoped `_count_unapproved_candidates` via `project_tags_match` when `projects` filter is active (`test_l5_deprecation_edits_only_frontmatter_and_l7_pending_count_is_scoped`). |

---

## 4. Phase 4 Engineering Tracks & Product Surfaces

### Track A: Corpus-Scale Lexical & Provenance Engine (`scripts/index_manifest.py`, `scripts/lexical_store.py`, `scripts/atom_mutator.py`, `scripts/evidence.py`, `scripts/scale_benchmark.py`)
- **Stat-Backed Revision Scans (`scripts/index_manifest.py`):** `atom_revisions()` scans via `os.scandir()` and caches stat-to-SHA-256 mappings and directory memos (`_DIR_MEMO`) once corpus size exceeds `LARGE_VAULT_STAT_CACHE_THRESHOLD = 64` (small test vaults continue unconditional raw-byte hashing).
- **Incremental FTS5 Upsert & Self-Healing (`scripts/lexical_store.py`):** `upsert_lexical_atoms()` updates only changed/deleted rows in `lexical.sqlite3` under `writer.lock` and falls back to full atomic rebuild on schema or row-count drift; `sync_lexical_index()` reconciles out-of-band external file changes; `list_indexed_atoms_fast()` serves paginated browse/search views directly from FTS5.
- **Provenance Caching:** `latest_decisions_by_atom()` (`scripts/atom_mutator.py`) caches decision-log maps and incrementally parses only appended JSONL tails; `_load_normalized_source()` (`scripts/evidence.py`) caches source normalization maps keyed strictly by raw-byte SHA-256.
- **Observed 1,500-Note Isolated Benchmark (`uv run --frozen python scripts/scale_benchmark.py --notes 1500 --queries 20`):**
  - Full FTS5 rebuild: `0.65s`
  - Cold freshness check: `257.3 ms` | Warm freshness check (P50): `7.3 ms`
  - Incremental single-note save (P50 / P95): `29.8 ms` / `48.0 ms`
  - Lexical query end-to-end (P50 / P95): `11.5 ms` / `13.2 ms`
  - Paginated 200-item browse: `31.0 ms`

### Track B: Zero-Friction Notes & One-Click Excerpt Verification (`scripts/notes_store.py`)
- **Separate Working Notes (`brain/notes/`):** Supports instant add/update/delete/search/backlinks/list (`second-brain notes {add,search,list,reindex,verify}`) indexed under `brain/.sidecar/notes.sqlite3` with CAS protection on edits/deletes.
- **Risk Quarantine:** Agent captures with risky patterns route via `_quarantine_agent_capture()` into `brain/0-raw/inbox/` and are never indexed into `notes.sqlite3`.
- **One-Click Excerpt Verification (`verify_note_excerpt()`):** Verifies note `expected_note_sha256`, writes an immutable raw-byte snapshot in `brain/0-raw/note-snapshots/`, computes the cryptographic source anchor (`find_evidence_anchor`), creates the atom in `brain/2-atoms/`, incrementally updates `lexical.sqlite3`, and owner-approves the atom via `apply_review_transition`.

### Track C: Streaming, Instant & Verified Chat (`scripts/streaming_chat.py`)
- Provides `instant` (direct verified claims + notes with zero model call), `verified` (structured claim selection validated through `validate_standard_answer`), and `stream` (tokenized synthesis followed by `check_citations` grounding audit) over `second-brain chat`, `POST /v1/chat/stream` (SSE `text/event-stream`), and `POST /v1/chat/warmup`.

### Track D: Dual-Token Loopback Product API (`scripts/api_product.py`)
- **Capability Split:** Dispatched from `scripts/memory_api.py` behind strict loopback, Host/Origin/Sec-Fetch-Site guards, and Bearer authentication.
  - **Agent/Bearer Routes:** `GET /v1/notes`, `GET /v1/notes/<filename>`, `GET /v1/notes/<filename>/backlinks`, `POST /v1/remember` (clean agent captures saved as `origin: agent_capture`; risky captures returned `202` and quarantined), `POST /v1/chat/stream`, `POST /v1/chat/warmup`, `GET /v1/atoms` (verified atoms only for non-owner callers), `GET /v1/playbooks`, `POST /v1/plan`.
  - **Owner-Token Gated Mutations (`owner_authorized`):** `POST /v1/notes`, `PUT /v1/notes/<filename>`, `DELETE /v1/notes/<filename>`, `POST /v1/notes/<filename>/verify`, `GET /v1/review/queue`, `POST /v1/atoms/<filename>/{approve,reject,demote,quarantine}`, `POST /v1/inbox/promote`, `POST /v1/inbox/reject`, `POST /v1/import`.

### Track E: Bulk Obsidian/Markdown Importer & IDE/Plugin Connectors (`scripts/bulk_import.py`, `scripts/ide_connector.py`, `scripts/mcp_server.py`, `plugins/`)
- **Idempotent Folder Importer (`import_folder()` / `second-brain import`):** Recursively imports `.md` vaults (skipping hidden/config directories and oversized payloads), tracks source SHA-256 digests in a manifest for incremental updates, uses `O_CREAT | O_EXCL` for new files and `atomic_write_bytes` for changed sources, and reconciles `notes.sqlite3`.
- **IDE / Plugin Bundles & Stdio Auth:**
  - `second-brain connect {add,remove,status}` (`scripts/ide_connector.py`) safely patches host MCP configs with timestamped JSON backups and installs `plugins/antigravity/second-brain/` (alongside `plugins/claude-code/second-brain/`).
  - `scripts/mcp_server.py` exposes `search_memory`, `get_atom`, `add_to_inbox`, `search_notes` (tagged `<owner_notes untrusted="true">`), `get_note` (tagged `untrusted="true"`), and `remember`, while `trusted_stdio_session()` requires both `BRAIN_MCP_TRUSTED_STDIO=1` and a constant-time match between `BRAIN_MCP_CLIENT_TOKEN` and the owner's configured MCP token.
  - Custom structured-output schema parameters (`json_schema`, `schema_name`, `gemini_response_schema` in `scripts/host_adapters.py`) support constrained Executive Planner drafts across local and opted-in online providers.
- **Desktop Companion, IDE Extension & Release Signing (`desktop/`, `extensions/vscode-second-brain/`, `scripts/sign_release.ps1`):**
  - Electron desktop companion (`desktop/main.js`, `desktop/preload.js`, `desktop/renderer/`, `desktop/package.json`) manages tray/capture windows and connects locally using security-directory tokens.
  - VS Code / Cursor / Windsurf extension (`extensions/vscode-second-brain/`) exposes Vault Explorer, Remember Selection (`Ctrl+Alt+M`), Quick Recall (`Ctrl+Alt+R`), and Streaming Chat.
  - Release signing helper (`scripts/sign_release.ps1`) handles Windows Authenticode signing (`.exe` / `.msi`) and `SHA256SUMS.txt` generation.

### 4B. Phase 4 Adversarial Security & Contract Audit Reconciliation

Following initial Phase 4 implementation, a full security and contract audit (`P4-HIGH-01` through `P4-MED-04`) hardened boundaries across storage, streaming chat, API/import surfaces, and desktop/IDE clients:

- **Note Frontmatter & Verification Integrity (`P4-HIGH-01`, `P4-HIGH-02`, `P4-MED-01`, `scripts/notes_store.py`):**
  - `render_note` enforces `origin` allowlisting against `NOTE_ORIGINS` (defaulting invalid origins to `"owner_import"`) and strips control/leading-dash characters from `created`/`updated` metadata to prevent YAML frontmatter injection.
  - `_quarantine_agent_capture` rejects reparse/symlink `inbox_dir` targets and collapses `--` runs in `source`.
  - `verify_note_excerpt` strips leading `#`, `>`, `*`, `-`, and spaces from claims, scans combined claim and excerpt payloads via `scan_inbox_payload_risks` (raising `NoteError`), rejects reparse/symlink snapshot/atom directories or snapshot files, and cleans up newly created orphan pending atoms and lexical index entries under `review_lock` when `apply_review_transition` raises `ReviewConflictError` or `ReviewTransitionError`.
  - `update_note` and `delete_note` guard `.history/` and `.trash/` paths against reparse/symlink traversal.
- **Streaming Chat Scope & Secret DLP (`P4-HIGH-03`, `scripts/streaming_chat.py`):**
  - `retrieve_context` enforces `project` scoping (`project:<scope>` or bare tag match) across retrieved note snippets rather than restricting only verified atoms.
  - `chat_events` scans outbound `verified` and `stream` prompts with `inspect_and_redact_outbound` (`fail_on_secret_detection=False`) before model invocation, emitting `error` + `done` events and auditing `"blocked_secret"` if triggered.
- **Product API Auditing & Bulk Import Symlink Hardening (`P4-HIGH-04`, `scripts/api_product.py`, `scripts/bulk_import.py`):**
  - `api_product.dispatch` resolves `memory_api._audit_api_event` so `/v1/notes`, `/v1/atoms`, `/v1/ide/*`, `owner_only` `403` denials, and `/v1/chat/stream` completions record entries to `memory-api-audit.jsonl`.
  - `bulk_import.py` checks `_is_reparse_or_link` on `source_root` before `.resolve()`, skips symlink/junction files and subdirectories during `iter_source_files`, and blocks symlink/reparse `notes_dir` or existing destination note targets.
- **IDE Connector Atomic Writes & CLI Contract (`P4-MED-02`, `scripts/ide_connector.py`):**
  - `_atomic_write_json` rejects symlinks/junctions, writes using OS-level binary/write/create/truncate flags with `os.fsync`, and applies `restrict_security_file_permissions` to written configs and `.bak-*` backup files.
  - `ide_connector.py` exposes `--json` (accepted before or after subcommands via `argparse.SUPPRESS` on subparsers), with `status --json` returning `{"targets": {<host>: {..., "name": label}}, "items": rows}`.
- **Schema Reconciliation & Desktop/Extension Client Hardening (`P4-MED-03`, `P4-MED-04`, `desktop/`, `extensions/vscode-second-brain/`):**
  - Reconciled API JSON payloads and SSE event fields between Python endpoints and TypeScript/JS clients (`items` alongside `notes`/`atoms`/`backlinks`/`total`, `sources`, `review_status`/`status`, `text`/`token`/`answer`/`reason`, and `total_ms`/`elapsed_ms`).
  - Hardened `desktop/main.js` (`apiRequest` restricted to `GET`/`POST`/`PUT`/`DELETE` and `/v1/` paths without whitespace/CR/LF; `ide:action` allowlisted to `status`/`add`/`remove` with non-dash `[a-z0-9-]+` targets; `setWindowOpenHandler` deny + `will-navigate` prevention on main and capture windows).
  - Replaced dynamic `innerHTML` assignments in `desktop/renderer/app.js` (`loadReviewQueue`, `loadIdeStatus`, `loadSystemView`) with `document.createElement` + `textContent`, and added `<meta http-equiv="Content-Security-Policy">` to the VS Code Streaming Chat webview.

---

## 5. Unified 10-Suite Release Verification (`scripts/test_all.py`)

The unified gate runs **10/10** suites end-to-end (`doctor`, `evaluation_dataset`, `audit_sources`, `system_safety_tests`, `phase2_safety_tests`, `phase3_cognitive_tests`, `phase4_product_tests`, `scale_benchmark_1500`, `chroma_rebuild_canary`, `smoke_test`) against an isolated runtime and temp vaults:

- `doctor` (CLI/environment/ABI/index health)
- `evaluation_dataset` (30-case dataset schema & SHA-256 binding)
- `audit_sources` (byte-offset & SHA-256 anchor audit)
- `system_safety_tests` (CAS, grounding, RRF/project scoping & security penetration suite)
- `phase2_safety_tests` (Phase 2 hardware tiers, inbox triage, consent/PII adapters, telemetry & API)
- `phase3_cognitive_tests` (10 tests: Phase 3A fitness/ranking, Playbooks, Executive Planner & H1/H2/M1–M4/L1–L7 regressions)
- `phase4_product_tests` (25 tests: Scale engine, notes, one-click verify, streaming chat, dual-token API, Obsidian import, IDE connectors, MCP auth & Phase 4 security/contract audit regressions)
- `scale_benchmark_1500` (1,500-note synthetic corpus correctness & latency canary)
- `chroma_rebuild_canary` (real ChromaDB staged rebuild, cosine activation, rollback & handle cleanup canary)
- `smoke_test` (end-to-end intake, grounding, lexical & dense recall smoke test)
