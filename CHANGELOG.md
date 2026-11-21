# Changelog

All notable changes to **Second Brain** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-11-22

Production `v1.0.0` completion release delivering **full core + gap-closure parity**: **Multimodal Voice Memo, Screenshot Paste & Media Upload (`/v1/ingest/media` + DLP Quarantine)**, **Git Post-Commit & Coding Session Memory Distiller (`second-brain hook`, `/v1/git/*`)**, **Bi-Temporal + Cross-Encoder Time-Travel Snapshot Recall & Plugin Schema Sync**, **Autonomous Watchdog Daemon (`--auto-sync` & Live Vector Upserts)**, **Portable Encrypted `.sbvault` Backup & Disaster-Recovery Restore (`second-brain backup`, `/v1/vault/backup/*`)**, **Bidirectional E2EE Mobile Drop & `SBMR1` Vault Recall/Search/Review PWA + On-Device Mobile Vault Client**, **Stage 4 Sandbox Executable Skills & Creative Media Provenance**, **Validated WinGet & Homebrew `Formula` Manifests**, and **90-Case Human-Curated + Live Third-Party Memory Benchmarks (`Mem0`, `FAISS`, `ChromaDB`, `rank-bm25`) + Sub-15ms 300-Atom ONNX Cross-Encoder Recall** across Python/CLI, Loopback API/SDK, 17-tool MCP, Electron Desktop, VS Code, and Streamlit Dashboard (`26/26` suites passing in `scripts/test_all.py`; see [v1.0.0 Independent Review](docs/independent-review-2026-11-22-v1.0.0.md) and [v1.0.0 Benchmark Report](docs/benchmark-report-v1.0.0.md)).

### Added
- **Multimodal Voice Memo, Screenshot Paste & Media Upload (`scripts/ingest_multimodal.py`, `POST /v1/ingest/media`, `MemoryApiClient.ingest_media`):**
  - Owner-gated raw binary (`application/octet-stream`) and base64 JSON media ingestion capped at `24 MiB` (`MAX_API_MEDIA_UPLOAD_BYTES`) without relaxing global JSON memory bounds on non-media endpoints.
  - Pillow image verification/sanitization, `faster-whisper` timestamped audio transcription, multi-format document extraction, and post-extraction DLP secret scrubbing with automatic `0-raw/inbox/` quarantine.
  - Clipboard screenshot paste (`Ctrl+V` / `Cmd+V`), `MediaRecorder` voice memo capture, and media upload across Electron Quick Capture (`capture.html`/`capture.js`) and `Import, Scout & Jobs`, VS Code (`secondBrain.ingestMedia`), and Streamlit Dashboard (`st.audio_input` + media uploader).
- **Git Post-Commit & Coding Session Memory Distiller (`scripts/git_distiller.py`, `second-brain hook`, `/v1/git/*`):**
  - Non-destructive managed `.git/hooks/post-commit` block (`# >>> second-brain-post-commit >>>`) preserving pre-existing user hooks, using space/metacharacter-safe POSIX single-quoting across editable checkouts, clean wheels, and Git worktrees.
  - Distills commits into `4-experiences/` and DLP-screened `0-raw/inbox/` captures while scrubbing outbound secrets before disk writes and rejecting option-injection/symlink traversal; surfaced across Desktop `IDE Hub`, VS Code (`secondBrain.installGitHook`, `secondBrain.uninstallGitHook`, `secondBrain.distillGitCommits`), and Dashboard.
- **Bi-Temporal + Cross-Encoder Snapshot Recall & MCP Plugin Parity (`scripts/time_travel.py`, `scripts/mcp_server.py`, `scripts/api_product.py`, `plugins/`):**
  - Point-in-time `as_of` & `include_expired` filtering and `score_query_atom_pairs()` Cross-Encoder reranking across `recall_at_snapshot()`, MCP `recall_at_snapshot`, `POST /v1/snapshots/recall`, `MemoryApiClient.snapshot_recall()`, and all 3 UIs.
  - Validity-window diffing (`old_valid_from`, `new_valid_from`, `old_valid_until`, `new_valid_until`) with strict non-owner redaction on revoked/superseded atoms, plus full `SKILL.md` schema alignment across Antigravity and Claude Code plugin bundles.
- **Watchdog Daemon Lifecycle (`scripts/watchdog_daemon.py`, `run_daemon.ps1`, `run_daemon.sh`, `/v1/daemon/*`):**
  - Live `O(|changed|)` Chroma vector upserts and `--auto-sync` / `--sync-interval` background E2EE scheduling inside `watchdog_daemon.py`, CLI flag forwarding in `run_daemon.ps1` / LF-clean `run_daemon.sh`, and `/v1/daemon/status` + `/v1/daemon/sync-once` controls across Desktop and Dashboard.
- **Portable Three-Store `.sbvault` Backup & Disaster-Recovery Restore (`scripts/vault_backup.py`, `second-brain backup`, `/v1/vault/backup/*`):**
  - `SBVBKP1` `AES-256-GCM` (`scrypt` + `HKDF-SHA256` with canonical JSON header AAD) or `--unencrypted` checksummed archives covering durable Three-Store Markdown and source/audit state while strictly excluding `.sidecar/`, `.chroma/`, locks, symlinks, and OS keys.
  - In-memory AEAD, per-file SHA-256, and Merkle tree verification (`verify`), safe disaster-recovery restore with automatic `.pre-restore-*.sbvault` safety archives on `--force`, and automatic SQLite FTS5 (`lexical.sqlite3` & `notes.sqlite3`) index rebuilds.
- **Distribution Manifests, Bidirectional Mobile Recall PWA & Live Third-Party Benchmarks (`scripts/build_distribution_manifests.py`, `scripts/inbox_drop.py`, `scripts/live_third_party_benchmark.py`, `docs/benchmark-report-v1.0.0.md`, `docs/independent-review-2026-11-22-v1.0.0.md`):**
  - Automated WinGet (`SecondBrain.SecondBrain.yaml`, `winget validate` PASS), concrete Homebrew `Formula` (`second-brain.rb`, real download URL + `sha256`, zero `:no_check`), bidirectional Mobile Drop & `SBMR1` Recall/Search/Review PWA (`second-brain-inbox-drop-pwa.zip`), and full On-Device Mobile Vault Client (`second-brain-mobile-vault-client.zip`) builders with strict semver boundary filtering and SHA-256 verification.
  - Expanded human-curated evaluation dataset to **90 cases (`second-brain-retrieval-curated-90`, `v3.0.0`)** (`1.000` trusted recall across lexical/dense/RRF, `0` trap FPs, `100%` answer grounding), added live head-to-head comparisons against real `mem0ai 2.2.1`, `faiss-cpu 1.16.0`, `chromadb 1.5.9`, `rank-bm25 0.2.2`, and `SQLite FTS5` (`docs/live-third-party-benchmark-2026-11-22.json`, `docs/sota-live-benchmark-results-2026-11-22.json`), optimized 300-atom ONNX Cross-Encoder total P95 to **`13.645 ms` (`< 15.0 ms` Tier-0 target)** (`docs/sota-benchmark-results-2026-11-21.json`), remediated 11/11 independent security audit findings (`docs/independent-review-2026-11-22-v1.0.0.md`), and verified **26/26 suites** in `scripts/test_all.py`.

## [0.5.0] — 2026-11-20

Complete neural, temporal, reasoning, and mobile quick-capture release delivering **4 end-to-end tracks**: **Verified ONNX Weights Downloader (`second-brain models pull`) + `[reranker]` Extra**, **`qwen3` Family Hardware Profiling & `<think>` Stripping**, **Bi-Temporal Fact Validity (`valid_from` / `valid_until` / `as_of`)**, and **Zero-Knowledge Mobile Inbox Drop (`SBID1` E2EE Static PWA + iOS Shortcut)** across Python/CLI, Loopback API/SDK, 17-tool MCP, Electron Desktop, VS Code, and Streamlit Dashboard (`22/22` suites passing).

### Added
- **Verified ONNX Weights Downloader & Explicit `[reranker]` Extra (`scripts/onnx_models.py`, `scripts/cross_encoder.py`, `scripts/knowledge_graph.py`, `pyproject.toml`):**
  - Dedicated `[reranker]` optional dependency extra (`onnxruntime`, `tokenizers`, `numpy`) while keeping the minimal core wheel lightweight.
  - SHA-256-pinned + immutable HuggingFace commit-pinned downloader (`second-brain models status|pull|verify|remove`, `/v1/models/onnx*`, `MemoryApiClient.*onnx*`) with DNS-pinned HTTPS redirect validation, `.models.lock`, CPU smoke inference, and atomic staging swap for `Xenova/ms-marco-MiniLM-L-6-v2` (int8 Cross-Encoder) and `Xenova/nli-deberta-v3-xsmall` (int8 NLI).
  - Dynamic `config.json` `id2label` resolution in `OnnxFastNliClassifier` and clause-aligned bidirectional ONNX NLI adjudication (`0` false positives across the live 78-atom vault; `1.000` Recall@5 and `0/10` traps).
- **`qwen3` Family Support, GGUF RAM-Fit Gating & `<think>` Stripping (`scripts/hardware_profile.py`, `scripts/streaming_chat.py`, `scripts/host_adapters.py`):**
  - Full `qwen3` / `qwen3.5` / `qwen3.6` / `qwen3-coder` tier catalog rankings combined with live `/api/tags` GGUF byte-size memory-fit gating (`_model_fits_host_memory` + `oversized_installed_models`) so oversized MoE/dense models never thrash host swap unless explicitly pinned via `BRAIN_GEN_MODEL`.
  - Automatic `think=False` request negotiation (`call_ollama_chat_maybe_no_think`) and stateful chunk-boundary `<think>...</think>` stripping (`_iter_strip_think_stream`, `strip_reasoning_think_blocks`) across streaming chat, JSON schema validation, Refinery, Editor, and NLI.
- **Bi-Temporal Fact Validity (`scripts/memory_format.py`, `scripts/atom_mutator.py`, `scripts/recall.py`, `scripts/knowledge_graph.py`, `scripts/mcp_server.py`):**
  - Optional ISO-8601 `valid_from` and `valid_until` frontmatter bounds with CAS-preserving `set_atom_temporal_bounds()` (`POST /v1/atoms/<name>/validity`, `review_atom.py set-validity`) that updates validity windows on approved atoms without invalidating current owner approval.
  - Default exclusion of expired/future atoms across `recall.py`, `mcp_server.py` (`search_memory` & `get_atom`), non-owner `GET /v1/atoms*`, `streaming_chat.py`, and `federated_recall()`, plus point-in-time `as_of` and `include_expired` queries.
  - Disjoint temporal-window contradiction suppression and `expired_validity` / `invalid_validity_window` drift alerts in `knowledge_graph.py`.
- **Zero-Knowledge Mobile Inbox Drop (`scripts/inbox_drop.py`, `mobile/inbox-drop/`, `scripts/vault_sync.py`):**
  - Forward-secret `SBID1\x00` envelope (`ECDH P-256 + HKDF-SHA256 + AES-256-GCM`) + revocable per-device `HMAC-SHA256` Encrypt-then-MAC (`second-brain sync drop-pair|drop-list|drop-revoke|pull-drops|export-pwa`, `/v1/sync/inbox-drop/*`) so mobile devices hold only the public `P-256` key and never see the vault master key or `Ed25519` signing key.
  - Self-contained offline-capable static WebCrypto PWA (`mobile/inbox-drop/`) + iOS Shortcut Share Sheet recipe with `#pair=` hash-scrubbing, client-side `AWS4-HMAC-SHA256` S3/R2 SigV4 `PUT` to `inbox-drops/<drop_id>.enc`, and automatic `sync_pull()` / `AutoSyncScheduler` decryption straight into quarantined `0-raw/inbox/mobile-drop-*.md`.
- **Suite 22/22 (`scripts/v050_sota_complete_tests.py`):** Comprehensive end-to-end & adversarial security verification across all 4 `v0.5.0` tracks.

## [0.4.0] — 2026-11-20

SOTA upgrade release completing **4 end-to-end engineering tracks**: **Retrieval & Model Upgrade**, **Real NLI & Auto-Linking Knowledge Graph**, **SOTA Browser Clipper & Layout/OCR PDF Extraction**, and **Production Cloud Sync (S3/R2 SigV4, OS Keychain/DPAPI, Auto-Sync & Interactive 3-Way Conflict Resolver)** across Python/CLI, Loopback API/SDK, Electron Desktop, VS Code, and Streamlit Dashboard (`21/21` suites passing).

### Added
- **Retrieval & Model Upgrade (`scripts/hardware_profile.py`, `scripts/rebuild_vectors.py`, `scripts/cross_encoder.py`, `scripts/chat_threads.py`):**
  - Auto-wired hardware-profile generation model selection (`qwen2.5-coder:7b` on Tier 2+ GPU workstations) honoring explicit `BRAIN_GEN_MODEL`, host policy pins, and per-request overrides.
  - `O(|changed|)` in-place Chroma/JSON vector upserts (`upsert_changed_vectors`) serialized under `writer.lock` so single-atom approvals, edits, merges, auto-links, and `sync_pull` updates keep `.chroma` `"current"` above 64 atoms without full re-embedding.
  - Local Cross-Encoder reranker (`OnnxCrossEncoder` + `< 1 ms` zero-network `InteractionCrossEncoder`) wired across lexical, semantic, RRF, and federated recall (`Recall@5 = 1.000`, `MRR = 0.975`, `0/10` traps).
  - Persistent owner-gated multi-turn chat history (`brain/.sidecar/chat_threads.sqlite3`) with recursive secret scrubbing, coreference/ellipsis follow-up expansion, and 1-click turn pinning to Working Notes across CLI, API/SDK, Desktop, VS Code, and Dashboard.
- **Real NLI & Auto-Linking Knowledge Graph (`scripts/knowledge_graph.py`, `scripts/notes_store.py`, `plugins/antigravity/`):**
  - Clause-aligned negated-stem conflict detection + fast local ONNX/predicate NLI pre-adjudicator (`nli_mode="llm"|"fast"|"hybrid"`, `0` false positives on live vault).
  - Strict project-scoped automatic `[[wikilink]]`, duplicate, and contradiction candidate linking during `verify_note_excerpt()` and vault-wide `auto_link_vault_atoms()` (`/v1/graph/auto-link`, `second-brain graph auto-link`), plus refreshed 17/17 Antigravity MCP plugin bundle.
- **SOTA Browser Clipper & Layout/OCR PDF Extraction (`extensions/browser-clipper/`, `scripts/web_clipper.py`, `scripts/doc_extractors.py`):**
  - Pure-DOM Readability-style semantic article scorer & HTML-to-Markdown converter (`lib/readability-markdown.js`) + server-side `trafilatura` clean-HTML fallback, plus 1-click single-use `SBC-XXXX-XXXX-XXXX` pairing codes (`/v1/clipper/pair-code` & `/v1/clipper/redeem`) with two-tier per-origin/per-IP rate limiting.
  - Coordinate-aware 2-column academic PDF de-interleaving, GFM pipe-table reconstruction, and Pillow-sanitized local OCR (`tesseract` / policy-gated loopback Vision) preserving exact `[page N]` UTF-8 byte-offset source anchors.
- **Production Cloud Sync (`scripts/keychain_store.py`, `scripts/vault_sync.py`, `scripts/watchdog_daemon.py`):**
  - Pure-Python RFC-compliant AWS S3 / Cloudflare R2 / Backblaze B2 SigV4 (`AWS4-HMAC-SHA256`) object backend (`s3_sigv4` over `PinnedHTTPTransport` for both path-style and virtual-hosted buckets) + signed-head-verified `sync gc` orphan pruning.
  - Native OS Keychain / Windows DPAPI secret sealing (`CryptProtectData`/`CryptUnprotectData` with entropy & heap zeroing, macOS `security -i`, Linux `secret-tool`) + `migrate_sync_key_to_keychain()`.
  - `AutoSyncScheduler` background sync loop (reconciled across Multi-Vault hot-switches, pausing automatically on conflicts) and interactive 3-way conflict resolution (`keep_local`, `accept_remote`, `keep_both`, `manual_merge` with `.sidecar/sync-base/` common-ancestor diffs, CAS, `.history/` backup, and canonical atom trust demotion) across all 3 UIs.
- **Suite 21/21 (`scripts/v040_sota_upgrades_tests.py`):** End-to-end verification and adversarial security tests across all 4 SOTA tracks.

## [0.3.0] — 2026-11-19

Advanced multi-device release completing **v0.3.0 Milestones 1–3**: **Zero-Knowledge E2EE Multi-Device Vault Sync**, **Local-First Browser Web Clipper (MV3 Chrome/Edge/Firefox)**, **Multi-Vault Workspaces & Opt-In Federated Recall**, complete **3-surface UI parity** across Electron Desktop, VS Code, and Streamlit Dashboard, hardened CI/release workflows, and the **20-suite unified verification gate**.

### Added
- **Zero-Knowledge E2EE Multi-Device Vault Sync (`scripts/vault_sync.py`, `second-brain sync`, `/v1/sync/*`, `MemoryApiClient.sync_*`):**
  - Authenticated encryption (`AES-256-GCM`, per-object `HKDF-SHA256` subkeys, random 96-bit nonces, strict AAD binding, `HMAC-SHA256` blind blob IDs) with `scrypt` passphrase KDF or 256-bit keyfile mode.
  - Per-device `Ed25519` signed heads with peer public-key pinning and monotonic anti-rollback epoch enforcement.
  - Strict Three-Store isolation (syncs only durable Markdown & append-only logs; never `.sidecar/`, `.chroma/`, symlinks, or security secrets; auto-rebuilds local lexical & notes indexes on pull).
  - Conflict-safe 3-way merge: deterministic set-union merge for append-only JSONL audit/lineage logs (`decision-record.jsonl`, `manifest.jsonl`, `atom-corrections.jsonl`, `mcp-audit.jsonl`), `.history/` backup before canonical fast-forward overwrites, and quarantined `0-raw/sync-conflicts/` copies on true divergence.
  - Pluggable `LocalFolderSyncBackend` (OneDrive/iCloud/Syncthing/NAS/USB) and DNS-pinned `HttpObjectSyncBackend` (`https://`, no redirects).
- **Local-First Browser Web Clipper (`extensions/browser-clipper/`, `scripts/web_clipper.py`, `scripts/build_clipper.py`, `second-brain clipper`, `/v1/clipper/*`):**
  - Cross-browser Manifest V3 extension (Chrome, Edge, Firefox) restricted strictly to `http://127.0.0.1/*` with zero cloud relay and zero `.innerHTML`.
  - Write-only scoped `clipper_*` bearer tokens stored exclusively as SHA-256 hashes under `SecondBrain/security/clipper-pairings.json` (`0600`/NTFS DACL), bound to owner-paired `chrome-extension://` / `moz-extension://` origins with strict `OPTIONS` CORS preflight allowlisting.
  - Verifiable web provenance via immutable UTF-8 source snapshots in `brain/0-raw/note-snapshots/clip-*.md` (SHA-256 digest + URL + timestamp) compatible with one-click `verify_note_excerpt` promotion, plus automatic server-side DLP secret & prompt-injection quarantine to `0-raw/inbox/`.
- **Multi-Vault Workspaces & Opt-In Federated Recall (`scripts/vault_workspaces.py`, `second-brain vault`, `/v1/vaults*`, `MemoryApiClient.*vault*`):**
  - Named vault registry (`SecondBrain/security/vaults.json`, `0600`/NTFS DACL) with non-overlapping directory enforcement, non-destructive unregister, atomic active hot-switching synced to `vault-path.txt` and live `MemoryLoopbackHTTPServer` runtime, and optional isolated per-vault bearer tokens (`vb_*`) that fail closed (`403`) against other vaults.
  - Strictly opt-in (`federated_searchable: false` by default), owner-gated read-only federated cross-vault search preserving independent vault approval, source-anchor, and preference filters.
- **Complete 3-Surface UI Parity:**
  - **Electron Desktop (`desktop/renderer/{index.html,app.js}`):** Sidebar Vault Quick-Switcher dropdown, Action-Item Tasks panel (`/v1/notes/tasks`), Local NLI Contradiction Radar opt-in (`nli=1`), E2EE Multi-Device Vault Sync controls, Multi-Vault Workspaces & Federated Search card, and Browser Web Clipper pairing panel under `System`.
  - **VS Code Extension (`extensions/vscode-second-brain/`):** Added `secondBrain.showTasks`, `secondBrain.scanContradictionsNli`, `secondBrain.syncVault`, `secondBrain.switchVault`, `secondBrain.federatedSearch`, and `secondBrain.pairClipper` commands.
  - **Streamlit Dashboard (`dashboard/app.py`):** Added `Action Items & Tasks` tab, Local NLI Adjudication toggle, E2EE Vault Sync controls, Multi-Vault Workspaces & Federated Search panel, and Browser Web Clipper pairing management.
- **Suites 19–20/20 Unified Verification Gate (`scripts/goal4_sync_and_ui_tests.py`, `scripts/goal5_clipper_and_workspaces_tests.py`):** Full behavioral & security tests for E2EE sync, MV3 Browser Clipper CORS/DLP/provenance/revocation, Multi-Vault isolation/hot-switching/per-vault tokens/federated recall, CLI commands, and 3-surface UI parity.

### Fixed
- Added `-SkipSign` switch and SHA-256 manifest fallback to `scripts/package_desktop.ps1` so unsigned CI Windows runs in `.github/workflows/release.yml` succeed cleanly alongside signed Authenticode builds.
- Added POSIX LF line-ending verification (`setup.sh`, `run_daemon.sh`, `scripts/package_desktop.sh`), `goal4_sync_and_ui_tests`, and `goal5_clipper_and_workspaces_tests` to `.github/workflows/ci.yml` and `.github/workflows/release.yml`.

## [0.2.0] — 2026-11-19

First unified production release consolidating **Core Runtime Parity & Three-Store Durability**, **Document Ingestion, Multi-Host DLP & 17-Tool MCP**, and **Cross-Platform Desktop/VS Code/Dashboard UI & Packaging**.

### Non-Negotiable Architecture Invariants Preserved
1. **Plain Markdown Truth:** Durable vault records live as human-readable Markdown under `brain/` (`notes/`, `2-atoms/`, `4-experiences/`, `5-playbooks/`).
2. **CAS Under `writer.lock`:** Every canonical mutation uses compare-and-swap SHA-256 verification and atomic temp-file writes under an OS advisory lock.
3. **Cryptographic Byte-Offset Anchoring:** Approved atoms bind verbatim quotes to exact UTF-8 byte offsets and source SHA-256 digests (`0-raw/` or `sources/`).
4. **Out-of-Model Grounding:** Factual answers are validated outside the LLM against live approved atom claims and anchored source bytes.
5. **Fail-Closed `127.0.0.1` Dual-Token Security:** Loopback-only HTTP/MCP surfaces with separate agent bearer tokens and owner-only governance tokens (`0600`/NTFS DACL).

### Added
- **Local Document Ingestion & Incremental Indexing:** Native PDF (`pypdf`), DOCX, and HTML extraction (`scripts/doc_extractors.py`) integrated into `second-brain import`, `ingest_multimodal.py`, `watchdog_daemon.py`, and Desktop drag-and-drop; chunked refinery pipeline and `O(changed)` incremental ChromaDB vector upserts.
- **17-Tool MCP Server & 6-Host Connectors:** Full MCP tool suite covering approved recall, working notes, Skill Playbooks, grounded Executive Planning, Contradiction Radar, Knowledge Graph, Time-Travel snapshots, and Merkle inclusion proofs; one-click connectors for Claude Code, Claude Desktop, Cursor, Windsurf, VS Code, and Antigravity (`second-brain connect`).
- **Multi-Host DLP Streaming Chat & Research View:** Provider-aware streaming chat with opt-in host consent, automatic DLP secret redaction, and explicit separation of owner-approved claims from unverified research leads.
- **Three-Store Durability & Content-Addressed Snapshots:** Durable `vault-ledger.jsonl` and `schema_version: 3` SHA-256 content-addressed snapshots stored in `brain/0-raw/archive/` (with transparent `.sidecar/` fallback/migration), Porter stemming, and generalized Merkle proofs covering both `2-atoms/` and `5-playbooks/`.
- **Semantic NLI Contradiction & Drift Radar + Comparative SOTA Benchmark:** Local NLI contradiction scoring in `knowledge_graph.py` and comparative benchmarking (`scripts/sota_benchmark.py`) across naive BM25 RAG, unscoped flat memory, and ungrounded synthesis.
- **Full UI Parity & Cross-Platform Packaging:** Unified Electron Desktop app (NSIS `.exe`, `.msi`, macOS `.dmg`/`.zip`, Linux `.AppImage`/`.deb`), VS Code extension (`.vsix`), Streamlit Dashboard, clean Python wheel (`.whl`), inner+outer Authenticode signing (`scripts/sign_release.ps1`), and automated CI/release workflows (`.github/workflows/ci.yml`, `.github/workflows/release.yml`).

### Fixed & Optimized
- **Unified Vault & Port Discovery:** Atomic `SecondBrain/security/api-port` persistence with PID liveness validation and unified `SecondBrain/security/vault-path.txt` resolution across Desktop, CLI, Dashboard, MCP, and VS Code.
- **Security Redaction & Audit Integrity:** Hardened non-owner `/v1/snapshots/diff` and `/v1/snapshots/recall` redaction (revoked/modified evidence, `playbook_changes`, deprecated/tampered playbooks, historical v1 claims), accurate `/v1/audit/sources*` counts, `outcome="denied"` MCP audit logging, and loopback no-proxy/no-redirect probes in `setup.ps1` / `setup.sh`.
- **Sub-15ms Recall & Paginated API Scale:** Single-term (`min(2, len(query_terms))`) and calibrated semantic recall, stopword stripping prior to FTS5 term slicing, bounded worker/SSE thread pools on `MemoryLoopbackHTTPServer`, FTS5-backed `GET /v1/atoms`, and verified-scan + source-anchor caching delivering `Recall@5 = 1.000`, `0/10` trap false positives, and `< 10 ms` P95 lexical recall on 220+ approved atoms.
- **Durable Feedback & Spec-Compliant Frontmatter:** Persisted `actor`/`note`/`feedback_kind` in durable `atom-corrections.jsonl` (decoupling supersession from negative penalties and removing ungrounded retrieval-hit inflation), spec-compliant YAML/JSON frontmatter parsing, and RAM/VRAM gating for oversized fallback models (`qwen3.6:latest`).

### Verification
- `second-brain doctor`: exit `0`
- `scripts/evaluate_retrieval.py`: `Recall@5 = 1.000`, `trusted_trap_false_positives = 0` (`0/10`), `lexical_p95_latency_ms = 8.58 ms`
- `scripts/test_all.py`: 18-suite unified release gate (Python safety/cognitive/product/god-features/Goal 2/Goal 3 contracts, Electron UI, VS Code extension, Streamlit AppTest, clean-wheel packaging, 1,500-note scale canary, Chroma rebuild canary, archive manifest canary, and live end-to-end smoke test)
- `dist/SHA256SUMS.txt`: verify downloaded release binaries with `sha256sum -c SHA256SUMS.txt` (macOS/Linux) or `Get-FileHash` (Windows PowerShell).
