# Second Brain

**Status:** Local-first prototype with explicit review and provenance gates. It is not a certified release. The vault contains durable Markdown; SQLite and vector stores are rebuildable indexes.

**Product goal:** a one-click installable Brain plugin with local/offline and online modes, plus adapters for owner-built AI hosts. Offline must be a first-class, state-of-the-art target, not a degraded fallback; online mode can add stronger models and other capabilities, with explicit controls over which context leaves the machine. Claims of outperforming every current product require head-to-head evaluation and are not established by this repository. Installation must profile each PC and report the capabilities that its hardware can actually support. This repository is not that product yet: it is a Windows-tested prototype with a CLI, Streamlit dashboard, local Ollama integration, and opt-in local MCP.

## Safety and readiness

- The dashboard binds to `127.0.0.1`, requires a local owner password, and enables Streamlit CORS/XSRF protections with a loopback host allowlist. It is still intended only for the local machine; do not expose it through a proxy or tunnel. The password verifier is stored under the current OS user's application state directory, not in the vault.
- MCP is disabled unless `BRAIN_ENABLE_LOCAL_MCP=1` is explicitly set. Do not register it with cloud-connected clients. See [the current system sweep](docs/system-sweep-2026-11-15.md).
- Model-generated atoms are candidates. The model cannot grant owner approval or citation status. Approval checks an exact evidence excerpt against a named local source and records the reviewed atom hash.
- Recall returns only the current atom revision whose latest owner decision is approval and whose source evidence still verifies. The current private vault has 14 atoms; `structured-logging.md` and `design-for-failure.md` are approved, while the other 12 remain unapproved.
- Chat surfaces buffer schema-constrained model drafts and render only exact current approved Claim text with a source-derived excerpt and program-generated citation. Approval uses the canonical decision log; dashboard history is revalidated before replay, and raw retrieval bodies are not shown in the factual chat. Unsupported paraphrases or invalid/stale references are withheld. A small three-repeat pilot answer run passed on `qwen2.5-coder:7b`; the configured default remains `llama3.2:3b`, which failed to answer either positive in one repeat. This is not broad model-quality evidence, and research leads are not supported.
- Trusted retrieval also requires at least two meaningful query terms to occur in the atom's Claim section. This conservative literal-overlap check blocks the current owner-personal trap queries, but it can miss valid synonym-only matches. It is a relevance guard, not proof that a claim answers a question.
- The SQLite FTS index is the model-free recall path. Dense recall is optional and requires a current cosine-distance index manifest. This Windows installation also needs Microsoft's Visual C++ v14 x64 runtime for Chroma's native bindings. Ollama is required for generation, embeddings, and model-assisted capture, not for lexical recall.
- The watchdog waits for stable files. Canonical ingestion writes share an OS lock, recheck the source hash before commit, and create new atom files without overwriting existing ones. Generated threads are also create-only.
- External web captures stay `web_remote` and quarantined until an owner action. The Scout rejects private targets and redirects and caps downloads.

## Project layout

```text
second_brain/
├── brain/                  durable Markdown vault and source evidence
│   ├── 0-raw/              captures, quarantined inbox/, archived originals, and note-snapshots/
│   ├── 1-desk/             working area
│   ├── 2-atoms/            canonical memory atoms and revision history
│   ├── 3-threads/          generated, non-overwriting synthesis files
│   ├── 4-experiences/      create-only task experience episodes (Cognitive Core)
│   ├── 5-playbooks/        owner-gated CAS Skill Playbooks and .history/ revisions
│   ├── notes/              zero-friction working notes (separate from verified atoms)
│   ├── sources/            external source pages and captured evidence
│   ├── .sidecar/           rebuildable FTS5 indexes (lexical, notes), telemetry WAL DB, and writer lock
│   └── .chroma/            optional rebuildable dense index
├── scripts/                ingestion, review, retrieval, planning, product API, and health tools
├── dashboard/              local Streamlit application
├── desktop/                Electron desktop companion (tray, capture & local API UI)
├── extensions/             VS Code / Cursor / Windsurf editor extension
├── plugins/                Antigravity and Claude Code MCP/skill plugin bundles
└── docs/                   architecture and operational notes
```

The root project directory is the Git boundary, but the live `brain/` vault is excluded from the code baseline because it contains owner-specific captures and memories. Back it up separately using an owner-controlled method. The existing local Git history still contains 18 old-layout vault files in its initial commit; there is no remote configured. Do not add a remote or push this history until those historical files have been reviewed or removed from history.

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

```powershell
uv run --locked second-brain doctor
uv run --locked second-brain profile
uv run --locked second-brain serve --port 8765
uv run --locked second-brain adapter-config --port 8765
uv run --locked second-brain playbooks list
uv run --locked second-brain plan --goal "<task goal>"
uv run --locked second-brain notes list
uv run --locked second-brain import <obsidian_or_markdown_dir>
uv run --locked second-brain chat "<question>" --mode verified
uv run --locked second-brain connect status
uv run --locked second-brain benchmark --notes 1500 --queries 20
uv run --locked second-brain sota-benchmark --atoms 300
uv run --locked second-brain ledger verify
uv run --locked second-brain ledger commit --actor <owner> --note "<checkpoint>"
uv run --locked second-brain graph contradictions
uv run --locked second-brain snapshot create --actor <owner> --label "<milestone>"
uv run --locked second-brain snapshot list
.\scripts\package_desktop.ps1 -Target nsis -SelfSigned
uv run --locked python scripts/memory_review.py --status all
uv run --locked python scripts/review_atom.py transition approve <atom.md> --expected-sha256 <sha256> --actor <owner>
uv run --locked python scripts/review_atom.py merge <survivor.md> <superseded.md> --survivor-sha256 <sha256> --superseded-sha256 <sha256> --actor <owner> --rationale "<reason>"
uv run --locked python scripts/inbox_workflow.py list
uv run --locked python scripts/host_adapters.py list
uv run --locked python scripts/cognitive_core.py fitness
uv run --locked python scripts/audit_sources.py
uv run --locked python scripts/rebuild_lexical_index.py
uv run --locked python scripts/rebuild_vectors.py
uv run --locked python scripts/test_all.py
```

`review_atom.py transition approve` refuses missing or ambiguous evidence. Editing an atom later invalidates the approval, even if its frontmatter still says `accepted`. Rebuild dense vectors only when the embedder is available; the rebuild is staged and records its model, dimensions, generation, source revisions, and cosine metric. Chroma’s collection metric is configured explicitly because its default single-node metric is squared L2, not cosine ([Chroma collection configuration](https://docs.trychroma.com/docs/collections/configure)).

`review_atom.py merge` requires explicit owner-selected survivor/superseded atoms, both current revision hashes, an actor, and a rationale. It preserves both revisions in `.history`, marks the survivor pending for re-review, excludes the merged atom from normal recall, refreshes lexical recall, and writes paired decision records. If a dense index is active, the audit marks it stale; rebuild dense vectors before dense recall resumes.

`audit_sources.py` reports current source-anchor and approval state without changing atoms. After reviewing its report, explicitly demote stale approvals with `uv run --locked python scripts/audit_sources.py --demote-stale --actor <owner>`; re-approve only after inspecting the exact current source.

Dataset `second-brain-retrieval-curated-30` (`v2.1.0`) evaluates 30 cases across `regression`, `development`, and `holdout` splits (see the [system sweep](docs/system-sweep-2026-11-15.md) for full metrics, model evaluation results, and hardware limits). Previously, pilot dataset `second-brain-retrieval-pilot` (`v2.0.0`) separated five candidate-ranking queries into two answerable cases, three exploratory pending-target cases, and two traps using dense generation `ce48706db17d440fbba04b8c7f86c37e`. Run `uv run --locked python scripts/evaluate_answers.py --model qwen2.5-coder:7b --runs 3` to repeat end-to-end answer checks. Raw candidate labels are not approved memories.

The answer run took 6.1 seconds p50 and 49.8 seconds p95 including cold start on this CPU-only host. The configured default `llama3.2:3b` did not answer either positive case in one repeat, so it is not yet a dependable default. These pilot results are too small to claim general model quality.

## Loopback Memory API, Host Adapters, and MCP policy

- **Loopback Memory API (`second-brain serve` / `scripts/memory_api.py` + `scripts/api_product.py`):** Binds strictly to `127.0.0.1` (default port `8765`), requires a 256-bit Bearer token (`--generate-token` or auto-provisioned under the OS user security state directory), enforces `Host`, `Origin`, and `Sec-Fetch-Site` browser rebinding/CSRF guards, exposes read-only approved recall (`POST /v1/recall`), grounded answers (`POST /v1/answer`), quarantined inbox capture (`POST /v1/inbox`), fitness telemetry (`GET /v1/cognitive/fitness`), episode logging (`POST /v1/cognitive/experiences`), owner-gated Skill Playbooks (`GET /v1/playbooks`), grounded Executive Planner (`POST /v1/plan`), zero-friction notes & SSE chat (`GET /v1/notes`, `POST /v1/remember`, `POST /v1/chat/stream`, `POST /v1/chat/warmup`), cryptographic Merkle audit ledger & proofs (`GET /v1/ledger/verify`, `GET /v1/ledger/prove/<atom>`), Contradiction & Drift Radar (`GET /v1/graph/contradictions`, `GET /v1/graph/summary`), Time-Travel snapshot diffs & point-in-time recall (`GET /v1/snapshots`, `POST /v1/snapshots/diff`, `POST /v1/snapshots/recall`), and owner-token-gated mutations (`/v1/notes` CRUD, `/v1/notes/<filename>/verify`, `/v1/review/queue`, `/v1/atoms/<filename>/{approve,reject,demote,quarantine}`, `/v1/inbox/{promote,reject}`, `/v1/import`, `POST /v1/ledger/commit`, `POST /v1/snapshots`), alongside the typed `MemoryApiClient` SDK. See [Phase 2 Architecture & Adversarial Security Sweep](docs/phase2-architecture-and-sweep-2026-11-15.md) and [Phase 3A & Phase 4 Architecture, Security Sweep & Scale Verification](docs/phase3a-and-phase4-architecture-2026-11-15.md).
- **Desktop Companion & VS Code Extension (`desktop/`, `extensions/vscode-second-brain/`, `scripts/package_desktop.ps1`):** Electron local UI (`Notes`, `Streaming Chat`, `Inbox & Review`, `Bulk Import`, `IDE Hub`, `System`, `Playbooks & Plan`, and `Vault Proof & Radar`) with global quick capture (`Ctrl+Shift+Space`), tray lifecycle controls (`Start`/`Restart`/`Stop Memory API`, `Open Vault Folder`, `Launch at Login`), automatic first-run `.venv` bootstrap, cross-platform installer targets (`nsis`, `msi`, `dmg`, `AppImage`, `deb`), and Windows Authenticode signing + `dist/SHA256SUMS.txt`. All 11 automated verification suites run via `uv run --locked python scripts/test_all.py`.
- **Per-Host Online & Custom AI Adapters (`scripts/host_adapters.py`):** Local loopback Ollama (`ollama_local`) is the only host enabled out of the box. Online providers (`openai_online`, `anthropic_online`, `gemini_online`) and `owner_custom_local` are disabled by default (`enabled: false`), require explicit owner consent and `project:<id>` allowlists in `host-sharing-policy.json`, scrub PII and block outbound secrets before opening any socket, enforce DNS-pinned `https://...:443` transport, and validate every draft claim outside the model via `scripts/answer_policy.py`.
- **MCP & IDE Connectors (`scripts/mcp_server.py`, `scripts/ide_connector.py`, `plugins/`):** MCP is not enabled globally by default; `second-brain connect` configures local IDEs/hosts (with timestamped backups and the Antigravity bundle under `plugins/antigravity/second-brain/`, alongside `plugins/claude-code/second-brain/`) using `BRAIN_MCP_TRUSTED_STDIO=1` paired with a constant-time `BRAIN_MCP_CLIENT_TOKEN` check (or HTTP `_meta` Bearer auth). Do not connect this vault to untrusted cloud hosts or expose the MCP process through a network service. Reads return verified approved atoms (`search_memory`, `get_atom`) or clearly separated `<owner_notes untrusted="true">` note results (`search_notes`, `get_note`), have no listable resource catalog, and route untrusted or risky `remember` / `add_to_inbox` inputs to the quarantined `brain/0-raw/inbox/` queue requiring owner promotion (`scripts/inbox_workflow.py`).

## Git safety

Use the project root as the single repository boundary. Review `git status` and the staged diff before committing. Do not use `git reset --hard` or `git clean` as rollback commands; restore only the specific files you intend to change, and preserve raw captures and atom history.
