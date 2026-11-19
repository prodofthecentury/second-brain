# Changelog

All notable changes to **Second Brain** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-11-19

Advanced multi-device release introducing **Zero-Knowledge E2EE Multi-Device Vault Sync**, complete **Action-Item Tasks / Local NLI Radar / E2EE Sync UI parity** across Electron Desktop, VS Code, and Streamlit Dashboard, hardened CI/release workflows, and the **19-suite unified verification gate**.

### Added
- **Zero-Knowledge E2EE Multi-Device Vault Sync (`scripts/vault_sync.py`, `second-brain sync`, `/v1/sync/*`, `MemoryApiClient.sync_*`):**
  - Authenticated encryption (`AES-256-GCM`, per-object `HKDF-SHA256` subkeys, random 96-bit nonces, strict AAD binding, `HMAC-SHA256` blind blob IDs) with `scrypt` passphrase KDF or 256-bit keyfile mode.
  - Per-device `Ed25519` signed heads with peer public-key pinning and monotonic anti-rollback epoch enforcement.
  - Strict Three-Store isolation (syncs only durable Markdown & append-only logs; never `.sidecar/`, `.chroma/`, symlinks, or security secrets; auto-rebuilds local lexical & notes indexes on pull).
  - Conflict-safe 3-way merge: deterministic set-union merge for append-only JSONL audit/lineage logs (`decision-record.jsonl`, `manifest.jsonl`, `atom-corrections.jsonl`, `mcp-audit.jsonl`), `.history/` backup before canonical fast-forward overwrites, and quarantined `0-raw/sync-conflicts/` copies on true divergence.
  - Pluggable `LocalFolderSyncBackend` (OneDrive/iCloud/Syncthing/NAS/USB) and DNS-pinned `HttpObjectSyncBackend` (`https://`, no redirects).
- **Complete 3-Surface UI Parity:**
  - **Electron Desktop (`desktop/renderer/{index.html,app.js}`):** Action-Item Tasks panel (`/v1/notes/tasks`), Local NLI Contradiction Radar opt-in (`nli=1`) displaying `exclusive_categories` & `nli_adjudication`, and E2EE Multi-Device Vault Sync controls (`Init`, `Join`, `Push`, `Pull`, `Verify`, conflict view) under `System`.
  - **VS Code Extension (`extensions/vscode-second-brain/`):** Added `secondBrain.showTasks`, `secondBrain.scanContradictionsNli`, and `secondBrain.syncVault` commands plus enriched Markdown formatters.
  - **Streamlit Dashboard (`dashboard/app.py`):** Added `Action Items & Tasks` tab under Working Notes, `Enable Local Ollama NLI Adjudication` toggle in Contradiction Radar, and E2EE Vault Sync controls under System Control.
- **Suite 19/19 Verification Gate (`scripts/goal4_sync_and_ui_tests.py`):** End-to-end 2-device sync, zero-knowledge leak checks, passphrase KDF fail-closed checks, JSONL set-union merge, conflict quarantine, ciphertext/AAD/Ed25519/rollback tamper rejection, mocked HTTPS object backend, owner-token API gating, UI parity, and CI/release pipeline hygiene.

### Fixed
- Added `-SkipSign` switch and SHA-256 manifest fallback to `scripts/package_desktop.ps1` so unsigned CI Windows runs in `.github/workflows/release.yml` succeed cleanly alongside signed Authenticode builds.
- Added POSIX LF line-ending verification (`setup.sh`, `run_daemon.sh`, `scripts/package_desktop.sh`) and `goal4_sync_and_ui_tests` to `.github/workflows/ci.yml` and `.github/workflows/release.yml`.

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
