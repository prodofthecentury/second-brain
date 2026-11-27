# Second Brain

Second Brain is a local-first memory store for one owner. Notes, clipped articles, PDFs, voice memos, and code history stay on disk. An optional local model (Ollama) can draft memory atoms from those captures. Chat, the dashboard, and MCP may cite an atom only after you approve that exact revision and its source excerpt still matches the file ("no source, no claim").

The package version in `pyproject.toml` is 1.0.0. That is not a claim that every screen, store listing, or model-backed path has been proven on every operating system. The check that runs in this repository is `scripts/test_all.py`. GitHub Actions runs it on Windows, macOS, and Linux (see `.github/workflows/ci.yml`). Tests that need a running Ollama, downloaded ONNX weights, or an optional extra are skipped when that dependency is absent. Set `BRAIN_REQUIRE_OLLAMA=1` (or `BRAIN_REQUIRE_ONNX`, `BRAIN_REQUIRE_WHISPER`, `BRAIN_REQUIRE_PYAV`, `BRAIN_REQUIRE_PILLOW`) when you want those tests to fail instead of skip.

`docs/independent-review-*.md` were written by the same automated process that produced the code. They are self-checks, not independent audits. Benchmark JSON files under `docs/` are historical local runs, not a current certification.

## What is in the tree

The Python modules live in the `scripts` package (`from scripts import recall`). They do not add that directory to `sys.path`. `python scripts/<tool>.py` still works: it re-enters as `python -m scripts.<tool>`. The `second-brain` command is unchanged.

The checkout keeps the full surface: the `second-brain` CLI, a loopback HTTP API, a 35-tool MCP server (disabled until `BRAIN_ENABLE_LOCAL_MCP=1`), a Streamlit dashboard, an Electron desktop app, a VS Code extension, a Manifest V3 browser clipper, and mobile clients (an inbox PWA and a Capacitor vault client). Apple notarization, store publishing, live models, and ONNX downloads need accounts, hardware, or weights this repository does not ship. Those paths run as far as they can without those secrets and stop with an explicit skip or error.

What belongs in a memory is written once in [docs/CAPTURE_POLICY.md](docs/CAPTURE_POLICY.md) and [docs/capture_policy.json](docs/capture_policy.json). Distillation, approval, and the MCP write tools load that file. A note or atom should be one durable checkable fact. Mood, fragments, secrets, and relative dates such as "tomorrow" do not make the cut. Uncertain facts stay tentative until you approve them. Coding agents should follow the same file; the local MCP server also serves it at `brain://policy/capture`.

Captured web articles land in `sources/` and are not searchable until you approve the current file bytes (`second-brain sources`, the dashboard quarantine tab, or `POST /v1/sources/approve`). Approval makes the text searchable as an approved source capture. It does not create a citable atom. A later refinery run can draft atoms; those still need their own approval.

Approvals made from the CLI require a short-lived owner grant (`second-brain owner enroll`, then `second-brain owner grant`). Passing `--actor owner` alone is rejected. The HTTP API records the fingerprint of the owner token that authorized the call. The dashboard records the unlocked password session. A library call that passes only an actor string still changes the file, and the decision log records `method: unattested_library_call` and `human_approval: false`. That record is not an owner approval.

This repository does not ship a personal daily-use vault. `brain/` is gitignored owner data. The tests build temporary vaults. Creative media, formal checks, and the other optional commands stay in the tree; they are extra surfaces on capture, approval, search, and MCP, and they are not deleted to make the core look smaller.

## Advanced modules

`second-brain doctor` reports these as OK or WARN. A missing tool is a warning, not a failure of the vault. The main CI job installs ffmpeg and espeak-ng on Linux and runs those tests for real. Windows OCR runs inside the Windows CI job. LoRA, Lean, Ollama, cloudflared, and tailscale run in `.github/workflows/advanced-runtime.yml`, which is separate from the five-job CI matrix.

| Module | Status |
| --- | --- |
| ffmpeg keyframes | Verified for real in the Linux CI job. A generated MP4 is decoded to a PNG. |
| espeak-ng speech | Verified for real in the Linux CI job. Kokoro ONNX weights are not downloaded there. |
| Kokoro TTS | Needs the Kokoro weight files. Doctor stays on WARN until `second-brain models pull kokoro-tts`. |
| Windows OCR | Verified for real on the windows-latest job. Other OS jobs skip it. |
| Screen capture | Verified for real on Linux with Xvfb and ffmpeg. |
| Code sandbox | Verified for real in the main suite. It runs a separate Python process with CPU and memory limits. |
| n8n client | Verified for real as HTTP against a loopback server. The n8n product is not installed. |
| Open WebUI tool | The generated tool module is compiled. Open WebUI itself is not booted. |
| LoRA training | A real PEFT loop on a tiny random GPT-2 runs in the advanced workflow. The main suite only checks the missing-dependency error unless torch, transformers, and peft are installed. |
| Lean | `lean` checks a real file in the advanced workflow. The main suite skips when `lean` is absent. |
| Ollama swarm debate | The advanced workflow pulls `qwen2.5:0.5b` and runs cited turns. Uncited sentences are dropped. |
| cloudflared and tailscale | The advanced workflow runs `--version`. It does not open an authenticated tunnel. That needs owner credentials. |
| Code sandbox, ledger, creative media | Already covered by the main suite. A GPU diffusion backend is not installed. |

## Speed, retrieval, remote models, and backups

Chat tokens already stream from `POST /v1/chat/stream`. The Streamlit dashboard paints each token into the reply, and the Electron app does the same through `window.secondBrain.streamChat` (`desktop/renderer/chat_send.js`). MCP tools still return one JSON result. The protocol used here has no token stream, so `search_memory` and `run_debate` are unchanged.

`BRAIN_OLLAMA_KEEP_ALIVE` (or `OLLAMA_KEEP_ALIVE`) is sent on Ollama chat and embed calls. The default is `30m`. Values that are not `-1` or a number plus `s`, `m`, or `h` fall back to `30m`. `BRAIN_OLLAMA_TIMEOUT` defaults to 120 seconds and is capped at 600. Doctor's reachability probe uses 3 seconds.

`OLLAMA_HOST` is still loopback only. A laptop can use Ollama on another machine by setting `BRAIN_OLLAMA_HOST` to an `http://` address on loopback or a private LAN range (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, or a private IPv6 address). Link-local addresses, including `169.254.169.254`, and public hosts are refused. Doctor prints a warning when the host is a private LAN address, because vault text is sent to that other machine. `BRAIN_GEN_MODEL=hermes3:8b` selects that model with no code change. Pull it on the machine that runs Ollama (`ollama pull hermes3:8b`). Chat, grounded answers, and the dashboard use that setting. MCP does not call the generator itself.

Embeddings are cached in SQLite by the model name and the SHA-256 of the text actually sent (after any redaction). Set `BRAIN_EMBED_CACHE=0` to turn the cache off, or `BRAIN_EMBED_CACHE_PATH` to move the file. A second embed of the same text does not call Ollama. This environment has no Ollama daemon, so the measured numbers below are local stand-ins, not model latency.

`second-brain capture --background` and `second-brain ingest --background` enqueue work and return. `second-brain jobs work` runs the queue. The database is `jobs.sqlite3` under the security-state directory (`BRAIN_JOB_QUEUE` overrides it). A job left in `running` is marked retry when the worker starts again. `second-brain jobs status` prints the counts. The dashboard System Control page shows the same counts. `GET /v1/jobs` requires the owner token.

`second-brain jobs work` also writes today's vault backup unless `BRAIN_AUTO_BACKUP=0`. `second-brain backup auto` does that without draining the queue. Set `BRAIN_BACKUP_PASSPHRASE` to at least 8 characters or the automatic archive is unencrypted. There is no operating-system timer; the backup runs when you run the worker or `backup auto`. `second-brain restore ARCHIVE --target DIR --unencrypted` (or a passphrase) and `--force` when the target is not empty. `second-brain backup create|verify|restore` is unchanged.

Hybrid retrieval still uses reciprocal rank fusion. `BRAIN_RRF_LEXICAL_WEIGHT` and `BRAIN_RRF_DENSE_WEIGHT` default to `1`, which is the previous equal blend. The reranker is the in-process interaction cross-encoder. If ONNX weights are present, doctor reports that backend; otherwise it reports the interaction fallback. Recall still requires the same literal overlap as before.

`second-brain eval PATH` scores a personal set and prints recall@k and MRR. A relevant label matches a filename, its slug, an atom id, a title, or the same content words. A dated id and a trailing content hash still match the stable slug. The example is `docs/personal-eval.example.json`. `second-brain eval-retrieval` and `second-brain eval-answers` are still there. A personal set is not the 90-query code-only dataset. `second-brain install` writes `house-rules.md` when it is missing, and distillation still runs if that file is later removed.

The dashboard graph drawing lives in `dashboard/graph_view.py` and is bound before the sidebar runs. The product route function lives in `scripts/api_dispatch.py` and is rebound onto `api_product` so existing patches see the same function body. SSE writing lives in `scripts/api_sse.py`. Desktop chat send lives in `desktop/renderer/chat_send.js`, loaded immediately after `app.js`.

Local measurements from one Linux agent machine, three runs, no Ollama process:

| What | Result |
| --- | --- |
| 200 short texts hashed twice by a local stand-in | 0.733–0.753 ms. This is not a model call. |
| Same 200 texts inserted in the embedding cache and read back | 159.9–181.8 ms. |
| Second pass, cache hit only | 14.2–14.4 ms. |
| Same text embedded twice through `embed_text` with a fake embedder | 1 model call, not 2. |
| Enqueue 50 capture jobs | 39.3–46.1 ms. |
| Claim those 50 jobs | 27.8–42.8 ms. |

A before/after of a real Ollama embed was not measured here.

## Install

Python 3.11, 3.12, or 3.13. From a source checkout, `./setup.sh` on Linux/macOS or `setup.ps1` on Windows installs `uv` when it can, syncs the lockfile, creates an empty vault, and builds the model-free SQLite index. Neither script installs Ollama. Pass `--pull-models` / `-PullModels` only when Ollama is already running. `./setup.sh --no-launch` and `setup.ps1 -NoLaunch` stop before opening the dashboard.

If `uv` is already installed:

```bash
uv sync --locked
uv run --locked second-brain install
uv run --locked second-brain doctor
```

Wheel installs store the vault in the platform user-data directory (`%LOCALAPPDATA%/SecondBrain/brain` on Windows, `~/Library/Application Support/SecondBrain/brain` on macOS, `$XDG_DATA_HOME/second-brain/brain` on Linux). Set `BRAIN_VAULT_DIR` to choose another location. Editable checkouts keep using their local `brain/` directory, which is gitignored because it holds owner data.

The doctor treats a missing lexical index as a failure. Missing Ollama models, a dense index, and zero approved memories are warnings: lexical recall does not need them.

Launch the dashboard with `./run_dashboard.sh` or `.\run_dashboard.ps1`. The first launch asks for a password of at least 12 characters. The session unlocks for 30 minutes. Reset it with `uv run --locked python scripts/dashboard_auth.py set-password`. The app listens on `http://127.0.0.1:8501`.

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
│   ├── 6-skills/           owner-promoted Stage 4 Sandboxed Executable Skills and .history/ revisions
│   ├── notes/              zero-friction working notes (separate from verified atoms)
│   ├── sources/            external source pages, captured evidence, and generated/ local creative media
│   ├── .sidecar/           strictly disposable rebuildable FTS5/notes/telemetry SQLite DBs & runtime locks
│   └── .chroma/            optional rebuildable dense index
├── scripts/                ingestion, review, retrieval, planning, executable skills, creative media, product API, and health tools
├── dashboard/              local Streamlit application
├── desktop/                Electron desktop companion (tray, capture & local API UI)
├── extensions/             VS Code / Cursor / Windsurf editor extension & Browser Web Clipper MV3
├── mobile/                 Full On-Device Mobile Vault Client (vault-client/ iOS/Android Capacitor + Offline PWA) & Inbox Drop PWA (inbox-drop/)
├── plugins/                Antigravity and Claude Code MCP/skill plugin bundles
└── docs/                   architecture notes and operator runbooks/templates (docs/runbooks/)
```

The root project directory is the Git boundary, and the live `brain/` vault is excluded from the code baseline because it contains owner-specific captures and memories. Back it up with `second-brain backup` or `second-brain backup auto`, and put it back with `second-brain restore`. Both `master` and `release-code-only` are 100% code-only across their entire commit history (the legacy 18-file initial vault commit was scrubbed from root history).

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
uv run --locked second-brain skills candidates
uv run --locked second-brain skills list
uv run --locked second-brain plan --goal "<task goal>"
uv run --locked second-brain mobile list
uv run --locked second-brain mobile export-bundle --out-dir dist
uv run --locked second-brain media status
uv run --locked second-brain media image --prompt "<prompt>" --style blueprint
uv run --locked second-brain media video --prompt "<prompt>" --format mp4
uv run --locked second-brain release check-credentials
uv run --locked second-brain gates status

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

# Owner approval grants, captured-source review, index rebuilds, and the full verification gate
.\scripts\package_desktop.ps1 -Target nsis -SelfSigned
uv run --locked python scripts/build_distribution_manifests.py --out-dir dist
uv run --locked python scripts/rebuild_lexical_index.py
uv run --locked python scripts/rebuild_vectors.py
uv run --locked second-brain owner enroll --actor "<your-name>"
uv run --locked second-brain owner grant
uv run --locked second-brain sources queue
uv run --locked second-brain sources approve-clean --actor "<your-name>" --grant "<grant>"
uv run --locked second-brain sources search "<query>"
uv run --locked python scripts/test_all.py
```

`review_atom.py transition approve` refuses missing or ambiguous evidence. Editing an atom later invalidates the approval, even if its frontmatter still says `accepted`. Rebuild dense vectors only when the embedder is available; the rebuild is staged and records its model, dimensions, generation, source revisions, and cosine metric. Chroma’s collection metric is configured explicitly because its default single-node metric is squared L2, not cosine ([Chroma collection configuration](https://docs.trychroma.com/docs/collections/configure)).

`review_atom.py merge` requires explicit owner-selected survivor/superseded atoms, both current revision hashes, an actor, and a rationale. It preserves both revisions in `.history`, marks the survivor pending for re-review, excludes the merged atom from normal recall, refreshes lexical recall, and writes paired decision records. If a dense index is active, the audit marks it stale; rebuild dense vectors before dense recall resumes.

`audit_sources.py` reports current source-anchor and approval state without changing atoms. After reviewing its report, explicitly demote stale approvals with `uv run --locked python scripts/audit_sources.py --demote-stale --actor <owner>`; re-approve only after inspecting the exact current source.

Dataset `second-brain-retrieval-curated-90` (`v3.0.0`, [docs/retrieval-evaluation-proposed.json](docs/retrieval-evaluation-proposed.json)) evaluates **90 agent-drafted queries (pending owner review)** (`25` standard factual positives pinned to SHA-256 atom revisions, `15` owner preference queries, `25` multi-atom & pending exploratory queries, and `25` adversarial/unapproved/cross-project/secret trap queries) partitioned evenly across `regression` (`30`), `development` (`30`), and `holdout` (`30`) splits over the 78-atom curated vault cohort (`75` owner-approved canonical atoms + `3` pending trap fixtures: `sodium-ion-battery-cost-record.md`, `code-reviews-as-mentorship.md`, and `continuous-integration-deployment.md`; see the [Owner Gate Review Packet](docs/owner-gate-review-packet.md)). See the [v1.0.0 Benchmark Report](docs/benchmark-report-v1.0.0.md), [live third-party curated benchmark](docs/live-third-party-benchmark-2026-11-22.json), [live SOTA + LLM benchmark](docs/sota-live-benchmark-results-2026-11-22.json), and [300-atom ONNX Cross-Encoder benchmark](docs/sota-benchmark-results-2026-11-21.json) (`4.00 ms` P50, `20.24–31.63 ms` P95 across 5 runs with median `24.22 ms` under host contention vs pre-contention `13.43–15.68 ms`, `100.0%` recall/precision/citation grounding, `0.0%` trap & cross-project leakage; Track 3 SLO sign-off tracked via `second-brain gates`). Run `uv run --locked python scripts/evaluate_answers.py --model qwen2.5-coder:7b --runs 2` to repeat end-to-end answer checks, or `uv run --with rank-bm25 --with faiss-cpu --with mem0ai python scripts/live_third_party_benchmark.py` for live third-party comparisons. Raw candidate labels are not approved memories.

On the 90-case curated suite (`50` scored answerable + trap cases), `qwen2.5-coder:7b` achieved `100.0%` expected claim coverage, `100.0%` citation grounding, and `0.0%` trap leakage at `23.63 s` P50 / `36.45 s` P95 (`36.89 s` max) on this 8-thread CPU-only host (`<= 45.0 s` recalibrated Tier-2 8-thread CPU SLO awaiting owner sign-off). Smaller 3B models (`llama3.2:3b`) remain fallback-only on Tier-1 hardware.

## Loopback Memory API, Host Adapters, and MCP policy

- **Loopback Memory API (`second-brain serve` / `scripts/memory_api.py` + `scripts/api_product.py`):** Binds strictly to `127.0.0.1` (default port `8765`, persisting active port discovery in `SecondBrain/security/api-port`) with bounded worker/SSE concurrency pools, requires a 256-bit Bearer token (`--generate-token` or auto-provisioned under the OS user security state directory), enforces `Host`, `Origin`, and `Sec-Fetch-Site` browser rebinding/CSRF guards, exposes read-only approved recall (`POST /v1/recall`), grounded answers (`POST /v1/answer`), quarantined inbox capture (`POST /v1/inbox`), fitness telemetry (`GET /v1/cognitive/fitness`), episode logging (`POST /v1/cognitive/experiences`), owner-gated Skill Playbooks (`GET /v1/playbooks`), Stage 4 Executable Skills (`GET /v1/skills`, `POST /v1/skills/*/execute`), Creative Media (`GET /v1/media/status`, `POST /v1/media/*`), grounded Executive Planner (`POST /v1/plan`), zero-friction notes & SSE chat (`GET /v1/notes`, `POST /v1/remember`, `POST /v1/chat/stream`, `POST /v1/chat/warmup`), cryptographic Merkle audit ledger & generalized atom/playbook/skill proofs (`GET /v1/ledger/verify`, `GET /v1/ledger/prove/<leaf>`), Contradiction & Drift Radar (`GET /v1/graph/contradictions`, `GET /v1/graph/summary`), Time-Travel snapshot diffs & point-in-time recall (`GET /v1/snapshots`, `POST /v1/snapshots/diff`, `POST /v1/snapshots/recall`), and owner-token-gated mutations (`/v1/notes` CRUD, `/v1/notes/<filename>/verify`, `/v1/review/queue`, `/v1/atoms/<filename>/{approve,reject,demote,quarantine}`, `/v1/inbox/{promote,reject}`, `/v1/import`, `POST /v1/ledger/commit`, `POST /v1/snapshots`), alongside the typed `MemoryApiClient` SDK. See [Phase 2 Architecture & Adversarial Security Sweep](docs/phase2-architecture-and-sweep-2026-11-15.md) and [Phase 3A & Phase 4 Architecture, Security Sweep & Scale Verification](docs/phase3a-and-phase4-architecture-2026-11-15.md).
- **Zero-Knowledge E2EE Multi-Device Vault Sync (`second-brain sync` / `scripts/vault_sync.py` / `/v1/sync/*`):** Owner-token-gated end-to-end encrypted sync (`AES-256-GCM`, per-object `HKDF-SHA256` subkeys, random 96-bit nonces, strict AAD binding, `HMAC-SHA256` keyed blind blob IDs, optional `scrypt` passphrase KDF or 256-bit keyfile mode, and `Ed25519` signed heads with peer key pinning and monotonic anti-rollback checks) over pluggable `local_folder` (OneDrive/iCloud/Syncthing/NAS/USB) or DNS-pinned `http_object` (`https://`) targets. Strictly enforces the Three-Store boundary (syncs only durable Markdown & append-only logs, never `.sidecar/`, `.chroma/`, or security secrets), merges append-only JSONL audit logs via deterministic set-union, backs up fast-forward canonical atom/playbook revisions in `.history/`, quarantines divergent regular-file conflicts into `0-raw/sync-conflicts/`, and automatically rebuilds local SQLite FTS5 indexes after pull.
- **Local-First Browser Web Clipper (`extensions/browser-clipper/`, `second-brain clipper`, `/v1/clipper/*`):** Cross-browser Manifest V3 extension (Chrome, Edge, Firefox) pinned strictly to `http://127.0.0.1/*` with write-only scoped `clipper_*` tokens (stored hashed in `clipper-pairings.json`), strict extension-origin CORS allowlisting, immutable UTF-8 SHA-256 source snapshots in `0-raw/note-snapshots/`, and automatic DLP/prompt-injection quarantine to `0-raw/inbox/`.
- **Multi-Vault Workspaces (`second-brain vault`, `scripts/vault_workspaces.py`, `/v1/vaults*`):** Isolated named vault registry (`SecondBrain/security/vaults.json`) with non-nested directory validation, atomic active hot-switching synced to `vault-path.txt` and live API runtime, optional isolated per-vault bearer tokens (`vb_*`), and strictly opt-in owner-gated read-only federated cross-vault recall.
- **Desktop Companion & VS Code Extension (`desktop/`, `extensions/vscode-second-brain/`, `scripts/package_desktop.ps1`):** Electron local UI (`Notes` + Action Items, `Streaming Chat` with `"research"` view, `Inbox & Review`, `Import, Scout & Jobs` + Multimodal Media/Voice/Paste Upload, `IDE Hub` + Git Post-Commit Distiller, `System` + ONNX Models + E2EE Sync + Mobile Inbox Drop + Multi-Vault Workspaces + Browser Web Clipper + Daemon Sync + Portable `.sbvault` Backup/Restore, `Playbooks & Plan`, `Vault Proof & Audit` with Local NLI Radar & Bi-Temporal/Cross-Encoder Snapshot Recall, `Knowledge Graph`, and `Vault Explorer`) with sidebar Vault Quick-Switcher, global quick capture (`Ctrl+Shift+Space` with screenshot paste & voice memo), tray lifecycle controls (`Start`/`Restart`/`Stop Memory API`, `Open Vault Folder`, `Launch at Login`), automatic first-run `.venv` bootstrap, cross-platform installer targets (`nsis`, `msi`, `dmg`, `AppImage`, `deb`), WinGet / Homebrew Cask manifests, and Windows Authenticode signing + `dist/SHA256SUMS.txt`. Run `uv run --locked python scripts/test_all.py` for the automated suites. Signing and notarization need the platform credentials; without them the packager skips those steps.
- **Per-Host Online & Custom AI Adapters (`scripts/host_adapters.py`):** Local loopback Ollama (`ollama_local`) is the only host enabled out of the box. Online providers (`openai_online`, `anthropic_online`, `gemini_online`) and `owner_custom_local` are disabled by default (`enabled: false`), require explicit owner consent and `project:<id>` allowlists in `host-sharing-policy.json`, scrub PII and block outbound secrets before opening any socket, enforce DNS-pinned `https://...:443` transport, and validate every draft claim outside the model via `scripts/answer_policy.py`.
- **35 MCP tools and IDE connectors (`scripts/mcp_server.py`, `scripts/ide_connector.py`, `plugins/`, [docs/mcp-local-policy.md](docs/mcp-local-policy.md)):** MCP is not enabled globally by default; `second-brain connect` configures local IDEs/hosts (with timestamped backups and the Antigravity bundle under `plugins/antigravity/second-brain/`, alongside `plugins/claude-code/second-brain/`) using `BRAIN_MCP_TRUSTED_STDIO=1` paired with a constant-time `BRAIN_MCP_CLIENT_TOKEN` check (or HTTP `_meta` Bearer auth) plus `BRAIN_MCP_ALLOWED_TOOLS` scoping. Do not connect this vault to untrusted cloud hosts or expose the MCP process through a network service. Reads return verified approved atoms (`search_memory`, `get_atom`), clearly separated `<owner_notes untrusted="true">` note results (`search_notes`, `get_note`), playbooks/skills/plan/graph/snapshot/ledger telemetry, and route untrusted or risky `remember`, `add_to_inbox`, or `distill_git_commits` inputs to quarantined `brain/0-raw/inbox/` requiring owner promotion (`scripts/inbox_workflow.py`).

## Git safety

Use the project root as the single repository boundary. Review `git status` and the staged diff before committing. Do not use `git reset --hard` or `git clean` as rollback commands; restore only the specific files you intend to change, and preserve raw captures and atom history.
