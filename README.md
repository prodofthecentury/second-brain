# Second Brain

**Status (`v1.0.0`):** Production local-first verifiable AI memory platform with SHA-256-pinned local ONNX Cross-Encoder & clause-aligned bidirectional DeBERTa NLI (`second-brain models pull`, `[reranker]` extra), GGUF-size-aware `qwen3` / `qwen2.5` hardware-tier profiling (`_model_fits_host_memory` + `<think>` stripping), Bi-Temporal Fact Validity (`valid_from` / `valid_until`, point-in-time `as_of`, Cross-Encoder Time-Travel Snapshot Recall, disjoint-window contradiction suppression & `expired_validity` drift radar), Multimodal Voice Memo, Clipboard Screenshot & Document Upload (`/v1/ingest/media`, `24 MiB` cap + Pillow/Whisper + DLP quarantine), Git Post-Commit & Coding Session Memory Distiller (`second-brain hook`, `/v1/git/*`), Portable Three-Store Encrypted `.sbvault` Backup & Disaster-Recovery Restore (`second-brain backup`, `/v1/vault/backup/*`), Zero-Knowledge Bidirectional Mobile Inbox Drop & Vault Recall (`SBID1` forward-secret `ECDH P-256 + HKDF-SHA256 + AES-256-GCM` drop + opt-in `SBMR1` `HKDF-SHA256 + AES-256-GCM` encrypted vault recall/BM25 search/review static PWA & full On-Device Mobile Vault Client), `O(|changed|)` single-atom ChromaDB vector upserts above 64 atoms, persistent owner-gated multi-turn chat threads (`chat_threads.sqlite3`) with coreference resolution, automatic `[[wikilink]]` graph linking, SOTA Browser Web Clipper (Readability-style Markdown DOM extraction + 1-click `SBC-*` pairing), coordinate-aware multi-column/table + local OCR PDF extraction (`.pdf`, `.docx`, `.epub`, `.html`, `.csv`/`.tsv`), Zero-Knowledge E2EE Cloud Sync (`AWS4-HMAC-SHA256` S3/R2/B2 SigV4, OS Keychain / Windows DPAPI secret sealing, `AutoSyncScheduler`, `sync gc`, and interactive 3-way conflict resolution), Multi-Vault Workspaces & Opt-In Federated Recall, Stage 4 Executable Skills sandbox, Creative Media provenance, dual-token Loopback HTTP API, native Electron desktop companion, 17-tool MCP & 6-host rule-installed IDE connectors, Skill Playbooks, validated WinGet / Homebrew `Formula` manifests, and cryptographic Merkle audit proofs (`26/26` release suites passing in `scripts/test_all.py`; see [v1.0.0 Independent Review](docs/independent-review-2026-11-22-v1.0.0.md) and [v1.0.0 Benchmark Report](docs/benchmark-report-v1.0.0.md)). Under the strict Three-Store boundary, the vault contains durable Markdown and append-only audit state (`brain/0-raw/archive/` holds durable decision logs, `vault-ledger.jsonl`, and `snapshots/`), while `brain/.sidecar/` and `brain/.chroma/` hold strictly disposable, rebuildable SQLite FTS5/notes/chat/telemetry DBs, dense vectors, and runtime locks.

**Product architecture:** One-click installable Second Brain across local/offline and opted-in online modes, with hardware capability profiling (`second-brain profile` / `doctor`), per-host outbound consent/PII/secret gates (`scripts/host_adapters.py`), IDE connectors (`second-brain connect`), VS Code `.vsix` extension, Browser Web Clipper MV3, bidirectional Mobile Drop & Recall PWA (`mobile/inbox-drop/`) + Mobile Vault Client (`mobile/vault-client/`), WinGet & Homebrew manifests, and cross-platform Electron desktop installer targets (`nsis`, `msi`, `dmg`, `AppImage`, `deb`). Offline lexical FTS5 is a first-class zero-dependency baseline; `scripts/live_third_party_benchmark.py` and `scripts/sota_benchmark.py` benchmark Second Brain (verified lexical + ONNX Cross-Encoder & hybrid ChromaDB HNSW RRF) head-to-head against live installed third-party memory engines (`Mem0` `mem0ai 2.2.1`, `FAISS` `faiss-cpu 1.16.0`, `ChromaDB 1.5.9`, `rank-bm25 0.2.2`, and `SQLite FTS5`) on both the **90-case human-curated vault dataset (`second-brain-retrieval-curated-90`, `v3.0.0`)** and **120–300 atom adversarial/multimodal corpora** with ingested PDF papers, 2-hop graph queries, bi-temporal expired traps, and drift/tamper traps (see [v1.0.0 Benchmark Report](docs/benchmark-report-v1.0.0.md), [live third-party curated benchmark](docs/live-third-party-benchmark-2026-11-22.json), and [300-atom ONNX latency benchmark](docs/sota-benchmark-results-2026-11-21.json)).

## Safety and readiness

- The dashboard binds to `127.0.0.1`, requires a local owner password, and enables Streamlit CORS/XSRF protections with a loopback host allowlist. It is still intended only for the local machine; do not expose it through a proxy or tunnel. The password verifier is stored under the current OS user's application state directory, not in the vault.
- MCP is disabled unless `BRAIN_ENABLE_LOCAL_MCP=1` is explicitly set. Do not register it with cloud-connected clients. See [the current system sweep](docs/system-sweep-2026-11-15.md).
- Model-generated atoms are candidates. The model cannot grant owner approval or citation status. Approval checks an exact evidence excerpt against a named local source and records the reviewed atom hash.
- Recall returns only the current atom revision whose latest owner decision is approval and whose source evidence still verifies. Clean checkouts start with zero atoms until notes or captures are verified and owner-approved; benchmark trap targets remain unapproved (`pending`) until reviewed by the owner.
- Chat surfaces (`second-brain chat`, Electron Desktop, VS Code, and Streamlit Dashboard) offer interactive modes—`instant` (`<20ms` direct verified claim & note cards with zero model call), `stream` (sentence-buffered synthesis over `[V#]`/`[N#]`/`[P#]` sources across local Ollama or opted-in online/custom hosts with secret blocking, indexed PII redaction/rehydration, and an out-of-model `check_citations` audit on every declarative sentence), `verified` (structured claim selection validated via `scripts/answer_policy.py`), and `research` (owner-approved grounded claims first plus a deterministic non-model-authored `[UNVERIFIED LEADS]` section for exploratory candidates without ever promoting leads to factual claims)—alongside strict schema-constrained audit answers (`generate_grounded_answer_via_host` + `answer_policy.py`), which buffer model drafts and render only exact current approved Claim text with a source-derived excerpt and program-generated citation. Approval uses the canonical decision log; dashboard strict-audit history is revalidated before replay, and unsupported paraphrases or invalid/stale references are withheld. On our 30-case multi-model evaluation (`N=5` passes, 100 runs/model), out-of-model validation achieved `100.0%` claim precision and `0.0%` trap leakage across both `qwen2.5-coder:7b` (`94.0%` positive coverage) and `llama3.2:3b` (`30.0%` positive coverage).
- Trusted retrieval also requires meaningful literal query-term overlap in the atom's `Claim` or `Evidence` section (at least two meaningful query terms when the query contains two or more terms). This conservative literal-overlap check blocks the current owner-personal trap queries, though it can miss purely synonym-based matches. It is a relevance guard, not proof that a claim answers a question.
- The SQLite FTS index is the model-free recall path. Dense recall is optional and requires a current cosine-distance index manifest. This Windows installation also needs Microsoft's Visual C++ v14 x64 runtime for Chroma's native bindings. Ollama is required for generation, embeddings, and model-assisted capture, not for lexical recall.
- The watchdog waits for stable files. Canonical ingestion writes share an OS lock, recheck the source hash before commit, and create new atom files without overwriting existing ones. Generated threads are also create-only.
- External web captures stay `web_remote` and quarantined until an owner action. The Scout rejects private targets and redirects and caps downloads.

## Project layout

```text
second_brain/
├── brain/                  durable Markdown vault and source evidence
│   ├── 0-raw/              captures, quarantined inbox/, note-snapshots/, and durable archive/ (decision logs, vault-ledger.jsonl & snapshots/)
│   ├── 1-desk/             working area
│   ├── 2-atoms/            canonical memory atoms and revision history
│   ├── 3-threads/          generated, non-overwriting synthesis files
│   ├── 4-experiences/      create-only task experience episodes (Cognitive Core)
│   ├── 5-playbooks/        owner-gated CAS Skill Playbooks and .history/ revisions
│   ├── notes/              zero-friction working notes (separate from verified atoms)
│   ├── sources/            external source pages and captured evidence
│   ├── .sidecar/           strictly disposable rebuildable FTS5/notes/telemetry SQLite DBs & runtime locks
│   └── .chroma/            optional rebuildable dense index
├── scripts/                ingestion, review, retrieval, planning, product API, and health tools
├── dashboard/              local Streamlit application
├── desktop/                Electron desktop companion (tray, capture & local API UI)
├── extensions/             VS Code / Cursor / Windsurf editor extension
├── plugins/                Antigravity and Claude Code MCP/skill plugin bundles
└── docs/                   architecture notes and operator runbooks/templates (docs/runbooks/)
```

The root project directory is the Git boundary, and the live `brain/` vault is excluded from the code baseline because it contains owner-specific captures and memories. Back it up separately using `second-brain backup` or an owner-controlled method. Both `master` and `release-code-only` are 100% code-only across their entire commit history (the legacy 18-file initial vault commit was scrubbed from root history).

## Start with the model-free baseline

The Python package requires Python 3.11. From a source checkout, run
`setup.ps1` on Windows, `./setup.sh` on Linux/macOS, or install `uv` and run
`uv sync --locked` followed by `uv run --locked second-brain install`. This
creates the empty vault structure and a model-free SQLite lexical index. Then
run `uv run --locked second-brain doctor` to see which optional capabilities
are installed. The private `brain/` vault is excluded from this repository;
restore it from your own backup if you want existing memories.

On Windows, `setup.ps1` installs `uv` with WinGet when available (falling back
to user-scoped pip when Python 3.11 exists), lets `uv` install managed Python
3.11, syncs the locked dependencies, initializes lexical recall, reports
capabilities, and starts the local dashboard (use `-NoLaunch` to stop after
setup). On Linux and macOS, `./setup.sh` performs the equivalent POSIX
bootstrap (`--no-launch` skips starting the dashboard). Neither script installs
the Ollama daemon itself; pass `-PullModels` / `--pull-models` only when a
local Ollama server is already running.
Wheel installs store the vault in
the platform user data directory (`%LOCALAPPDATA%/SecondBrain/brain` on
Windows, `~/Library/Application Support/SecondBrain/brain` on macOS, or
`$XDG_DATA_HOME/second-brain/brain` on Linux); set `BRAIN_VAULT_DIR` to choose
another location. Editable source checkouts keep using their local `brain/`
directory.

From PowerShell in the project directory:

```powershell
uv run --locked python scripts/rebuild_lexical_index.py
uv run --locked python scripts/doctor.py
```

The doctor treats a missing or stale lexical index as a failure. Missing Ollama models, dense indexing, and zero currently approved memories are reported as warnings because they affect optional capabilities or available content, not the lexical engine itself.

Launch the dashboard with:

```powershell
.\run_dashboard.ps1
```

The dashboard, CLI, watchdog, MCP, Scout, and evaluation tools are included in
the core locked dependencies (`uv sync --locked`); only `multimodal` capture
(`uv sync --locked --extra multimodal`) is an optional extra, and none of the
model-backed components are required for offline lexical recall. Ollama must be
installed separately to enable local model-backed chat, embeddings, and capture.

On first launch, the script prompts for a 12-character minimum owner password and confirmation. The browser session unlocks for up to 30 minutes of activity and can be locked from the sidebar. Reset it with `uv run --locked python scripts/dashboard_auth.py set-password`. The app is available at `http://127.0.0.1:8501`. The dashboard’s atom editor uses compare-and-swap, resets trust to pending after edits, and rebuilds lexical recall before reporting success. Approve only after inspecting the exact revision and its source excerpt.

This is a local single-owner gate, not protection from malware or another process running as the same OS user: that user can read the vault and can replace the application. Remote/proxy/tunnel use remains unsupported. Streamlit's native login is OIDC-only and requires an identity provider, so the offline dashboard uses its own salted `scrypt` verifier instead. CORS and XSRF are both enabled because Streamlit documents that XSRF alone does not protect WebSocket origin checks.

See [the dashboard security boundary](docs/dashboard-security.md) for the threat model, credential location, tested controls, and remaining cross-origin verification.

## Common commands

The unified `second-brain` CLI exposes daily capture, notes, chat, review, auditing, planning, Merkle proofs, IDE connectors, and Loopback API operations:

```powershell
# Health, hardware capability tier, bootstrap & local dashboard
uv run --locked second-brain doctor
uv run --locked second-brain profile
uv run --locked second-brain install
uv run --locked second-brain dashboard

# Zero-friction working notes, quick capture, bulk import & interactive chat
uv run --locked second-brain capture "<thought or note>" --tags "project:second_brain"
uv run --locked second-brain notes add --title "<title>" --body "<markdown>"
uv run --locked second-brain notes list
uv run --locked second-brain notes search "<query>"
uv run --locked second-brain notes verify <note.md> --excerpt "<exact excerpt>" --actor <owner>
uv run --locked second-brain import <obsidian_or_markdown_dir>
uv run --locked second-brain chat "<question>" --mode stream   # modes: instant | stream | verified | research

# Loopback HTTP API, IDE/MCP connectors & host sharing policies
uv run --locked second-brain serve --port 8765
uv run --locked second-brain connect status                    # add/remove: claude-code, antigravity, cursor, windsurf, vscode, claude-desktop
uv run --locked second-brain adapter-config --port 8765
uv run --locked second-brain hosts list

# Canonical CAS review, source anchor audit, Skill Playbooks & Executive Planner
uv run --locked second-brain review transition approve <atom.md> --expected-sha256 <sha256> --actor <owner>
uv run --locked second-brain review merge <survivor.md> <superseded.md> --survivor-sha256 <sha256> --superseded-sha256 <sha256> --actor <owner> --rationale "<reason>"
uv run --locked second-brain audit
uv run --locked second-brain inbox list
uv run --locked second-brain cognitive fitness
uv run --locked second-brain playbooks list
uv run --locked second-brain plan --goal "<task goal>"

# Cryptographic Merkle ledger, Contradiction Radar, Time-Travel snapshots, E2EE Sync & benchmarks
uv run --locked second-brain ledger verify
uv run --locked second-brain ledger commit --actor <owner> --note "<checkpoint>"
uv run --locked second-brain graph contradictions
uv run --locked second-brain snapshot create --actor <owner> --label "<milestone>"
uv run --locked second-brain snapshot list
uv run --locked second-brain sync init <sync_folder_or_https_url>
uv run --locked second-brain sync push
uv run --locked second-brain sync pull
uv run --locked second-brain sync verify
uv run --locked second-brain clipper pair --origin chrome-extension://<32-char-id>
uv run --locked second-brain vault list
uv run --locked second-brain vault create "Work" <path> --isolated-token
uv run --locked second-brain vault switch work
uv run --locked second-brain models pull
uv run --locked second-brain hook install --repo .
uv run --locked second-brain hook distill --repo . --max-commits 5
uv run --locked second-brain backup create ..\vault-backup.sbvault
uv run --locked second-brain backup verify ..\vault-backup.sbvault
uv run --locked second-brain benchmark --notes 1500 --queries 20
uv run --locked second-brain sota-benchmark --atoms 300

# Desktop installer packaging, distribution manifests, index rebuilds & full 26-suite verification gate
.\scripts\package_desktop.ps1 -Target nsis -SelfSigned
uv run --locked python scripts/build_distribution_manifests.py --out-dir dist
uv run --locked python scripts/rebuild_lexical_index.py
uv run --locked python scripts/rebuild_vectors.py
uv run --locked python scripts/test_all.py
```

`review_atom.py transition approve` refuses missing or ambiguous evidence. Editing an atom later invalidates the approval, even if its frontmatter still says `accepted`. Rebuild dense vectors only when the embedder is available; the rebuild is staged and records its model, dimensions, generation, source revisions, and cosine metric. Chroma’s collection metric is configured explicitly because its default single-node metric is squared L2, not cosine ([Chroma collection configuration](https://docs.trychroma.com/docs/collections/configure)).

`review_atom.py merge` requires explicit owner-selected survivor/superseded atoms, both current revision hashes, an actor, and a rationale. It preserves both revisions in `.history`, marks the survivor pending for re-review, excludes the merged atom from normal recall, refreshes lexical recall, and writes paired decision records. If a dense index is active, the audit marks it stale; rebuild dense vectors before dense recall resumes.

`audit_sources.py` reports current source-anchor and approval state without changing atoms. After reviewing its report, explicitly demote stale approvals with `uv run --locked python scripts/audit_sources.py --demote-stale --actor <owner>`; re-approve only after inspecting the exact current source.

Dataset `second-brain-retrieval-curated-90` (`v3.0.0`, [docs/retrieval-evaluation-proposed.json](docs/retrieval-evaluation-proposed.json)) evaluates **90 human-curated queries** (`25` standard factual positives pinned to SHA-256 atom revisions, `15` owner preference queries, `25` multi-atom & pending exploratory queries, and `25` adversarial/unapproved/cross-project/secret trap queries) partitioned evenly across `regression` (`30`), `development` (`30`), and `holdout` (`30`) splits over the 78-atom vault (`75` owner-approved, `3` pending traps). See the [v1.0.0 Benchmark Report](docs/benchmark-report-v1.0.0.md), [live third-party curated benchmark](docs/live-third-party-benchmark-2026-11-22.json), [live SOTA + LLM benchmark](docs/sota-live-benchmark-results-2026-11-22.json), and [300-atom ONNX Cross-Encoder benchmark](docs/sota-benchmark-results-2026-11-21.json) (`13.645 ms` total P95 `< 15.0 ms` Tier-0 target, `100.0%` recall/precision/citation grounding, `0.0%` trap & cross-project leakage). Run `uv run --locked python scripts/evaluate_answers.py --model qwen2.5-coder:7b --runs 2` to repeat end-to-end answer checks, or `uv run --with rank-bm25 --with faiss-cpu --with mem0ai python scripts/live_third_party_benchmark.py` for live third-party comparisons. Raw candidate labels are not approved memories.

On the 90-case curated suite (`50` scored answerable + trap cases), `qwen2.5-coder:7b` achieved `100.0%` expected claim coverage, `100.0%` citation grounding, and `0.0%` trap leakage at `25.23 s` P50 / `38.86 s` P95 on this 8-thread CPU-only host (`<= 45.0 s` Tier-2 8-thread CPU SLO). Smaller 3B models (`llama3.2:3b`) remain fallback-only on Tier-1 hardware.

## Loopback Memory API, Host Adapters, and MCP policy

- **Loopback Memory API (`second-brain serve` / `scripts/memory_api.py` + `scripts/api_product.py`):** Binds strictly to `127.0.0.1` (default port `8765`, persisting active port discovery in `SecondBrain/security/api-port`) with bounded worker/SSE concurrency pools, requires a 256-bit Bearer token (`--generate-token` or auto-provisioned under the OS user security state directory), enforces `Host`, `Origin`, and `Sec-Fetch-Site` browser rebinding/CSRF guards, exposes read-only approved recall (`POST /v1/recall`), grounded answers (`POST /v1/answer`), quarantined inbox capture (`POST /v1/inbox`), fitness telemetry (`GET /v1/cognitive/fitness`), episode logging (`POST /v1/cognitive/experiences`), owner-gated Skill Playbooks (`GET /v1/playbooks`), grounded Executive Planner (`POST /v1/plan`), zero-friction notes & SSE chat (`GET /v1/notes`, `POST /v1/remember`, `POST /v1/chat/stream`, `POST /v1/chat/warmup`), cryptographic Merkle audit ledger & generalized atom/playbook proofs (`GET /v1/ledger/verify`, `GET /v1/ledger/prove/<leaf>`), Contradiction & Drift Radar (`GET /v1/graph/contradictions`, `GET /v1/graph/summary`), Time-Travel snapshot diffs & point-in-time recall (`GET /v1/snapshots`, `POST /v1/snapshots/diff`, `POST /v1/snapshots/recall`), and owner-token-gated mutations (`/v1/notes` CRUD, `/v1/notes/<filename>/verify`, `/v1/review/queue`, `/v1/atoms/<filename>/{approve,reject,demote,quarantine}`, `/v1/inbox/{promote,reject}`, `/v1/import`, `POST /v1/ledger/commit`, `POST /v1/snapshots`), alongside the typed `MemoryApiClient` SDK. See [Phase 2 Architecture & Adversarial Security Sweep](docs/phase2-architecture-and-sweep-2026-11-15.md) and [Phase 3A & Phase 4 Architecture, Security Sweep & Scale Verification](docs/phase3a-and-phase4-architecture-2026-11-15.md).
- **Zero-Knowledge E2EE Multi-Device Vault Sync (`second-brain sync` / `scripts/vault_sync.py` / `/v1/sync/*`):** Owner-token-gated end-to-end encrypted sync (`AES-256-GCM`, per-object `HKDF-SHA256` subkeys, random 96-bit nonces, strict AAD binding, `HMAC-SHA256` keyed blind blob IDs, optional `scrypt` passphrase KDF or 256-bit keyfile mode, and `Ed25519` signed heads with peer key pinning and monotonic anti-rollback checks) over pluggable `local_folder` (OneDrive/iCloud/Syncthing/NAS/USB) or DNS-pinned `http_object` (`https://`) targets. Strictly enforces the Three-Store boundary (syncs only durable Markdown & append-only logs, never `.sidecar/`, `.chroma/`, or security secrets), merges append-only JSONL audit logs via deterministic set-union, backs up fast-forward canonical atom/playbook revisions in `.history/`, quarantines divergent regular-file conflicts into `0-raw/sync-conflicts/`, and automatically rebuilds local SQLite FTS5 indexes after pull.
- **Local-First Browser Web Clipper (`extensions/browser-clipper/`, `second-brain clipper`, `/v1/clipper/*`):** Cross-browser Manifest V3 extension (Chrome, Edge, Firefox) pinned strictly to `http://127.0.0.1/*` with write-only scoped `clipper_*` tokens (stored hashed in `clipper-pairings.json`), strict extension-origin CORS allowlisting, immutable UTF-8 SHA-256 source snapshots in `0-raw/note-snapshots/`, and automatic DLP/prompt-injection quarantine to `0-raw/inbox/`.
- **Multi-Vault Workspaces (`second-brain vault`, `scripts/vault_workspaces.py`, `/v1/vaults*`):** Isolated named vault registry (`SecondBrain/security/vaults.json`) with non-nested directory validation, atomic active hot-switching synced to `vault-path.txt` and live API runtime, optional isolated per-vault bearer tokens (`vb_*`), and strictly opt-in owner-gated read-only federated cross-vault recall.
- **Desktop Companion & VS Code Extension (`desktop/`, `extensions/vscode-second-brain/`, `scripts/package_desktop.ps1`):** Electron local UI (`Notes` + Action Items, `Streaming Chat` with `"research"` view, `Inbox & Review`, `Import, Scout & Jobs` + Multimodal Media/Voice/Paste Upload, `IDE Hub` + Git Post-Commit Distiller, `System` + ONNX Models + E2EE Sync + Mobile Inbox Drop + Multi-Vault Workspaces + Browser Web Clipper + Daemon Sync + Portable `.sbvault` Backup/Restore, `Playbooks & Plan`, `Vault Proof & Audit` with Local NLI Radar & Bi-Temporal/Cross-Encoder Snapshot Recall, `Knowledge Graph`, and `Vault Explorer`) with sidebar Vault Quick-Switcher, global quick capture (`Ctrl+Shift+Space` with screenshot paste & voice memo), tray lifecycle controls (`Start`/`Restart`/`Stop Memory API`, `Open Vault Folder`, `Launch at Login`), automatic first-run `.venv` bootstrap, cross-platform installer targets (`nsis`, `msi`, `dmg`, `AppImage`, `deb`), WinGet / Homebrew Cask manifests, and Windows Authenticode signing + `dist/SHA256SUMS.txt`. All 26 automated verification suites run via `uv run --locked python scripts/test_all.py`.
- **Per-Host Online & Custom AI Adapters (`scripts/host_adapters.py`):** Local loopback Ollama (`ollama_local`) is the only host enabled out of the box. Online providers (`openai_online`, `anthropic_online`, `gemini_online`) and `owner_custom_local` are disabled by default (`enabled: false`), require explicit owner consent and `project:<id>` allowlists in `host-sharing-policy.json`, scrub PII and block outbound secrets before opening any socket, enforce DNS-pinned `https://...:443` transport, and validate every draft claim outside the model via `scripts/answer_policy.py`.
- **MCP & IDE Connectors (`scripts/mcp_server.py`, `scripts/ide_connector.py`, `plugins/`):** MCP is not enabled globally by default; `second-brain connect` configures local IDEs/hosts (with timestamped backups and the Antigravity bundle under `plugins/antigravity/second-brain/`, alongside `plugins/claude-code/second-brain/`) using `BRAIN_MCP_TRUSTED_STDIO=1` paired with a constant-time `BRAIN_MCP_CLIENT_TOKEN` check (or HTTP `_meta` Bearer auth). Do not connect this vault to untrusted cloud hosts or expose the MCP process through a network service. Reads return verified approved atoms (`search_memory`, `get_atom`) or clearly separated `<owner_notes untrusted="true">` note results (`search_notes`, `get_note`), have no listable resource catalog, and route untrusted or risky `remember` / `add_to_inbox` inputs to the quarantined `brain/0-raw/inbox/` queue requiring owner promotion (`scripts/inbox_workflow.py`).

## Git safety

Use the project root as the single repository boundary. Review `git status` and the staged diff before committing. Do not use `git reset --hard` or `git clean` as rollback commands; restore only the specific files you intend to change, and preserve raw captures and atom history.
