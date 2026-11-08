# Second Brain

**Status:** Local-first prototype with explicit review and provenance gates. It is not a certified release. The vault contains durable Markdown; SQLite and vector stores are rebuildable indexes.

**Product goal:** a one-click installable Brain plugin with local/offline and online modes, plus adapters for owner-built AI hosts. Offline must be a first-class, state-of-the-art target, not a degraded fallback; online mode can add stronger models and other capabilities, with explicit controls over which context leaves the machine. Claims of outperforming every current product require head-to-head evaluation and are not established by this repository. Installation must profile each PC and report the capabilities that its hardware can actually support. This repository is not that product yet: it is a Windows-tested prototype with a CLI, Streamlit dashboard, local Ollama integration, and opt-in local MCP.

## Safety and readiness

- The dashboard binds to `127.0.0.1`, requires a local owner password, and enables Streamlit CORS/XSRF protections with a loopback host allowlist. It is still intended only for the local machine; do not expose it through a proxy or tunnel. The password verifier is stored under the current OS user's application state directory, not in the vault.
- MCP is disabled unless `BRAIN_ENABLE_LOCAL_MCP=1` is explicitly set. Do not register it with cloud-connected clients. See [the current system sweep](docs/system-sweep-2026-10-27.md).
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
│   ├── 0-raw/              captures and archived originals
│   ├── 1-desk/             working area
│   ├── 2-atoms/            canonical memory atoms and revision history
│   ├── 3-threads/          generated, non-overwriting synthesis files
│   ├── sources/            external source pages and captured evidence
│   ├── .sidecar/           rebuildable lexical index and writer lock
│   └── .chroma/            optional rebuildable dense index
├── scripts/                ingestion, review, retrieval, and health tools
├── dashboard/              local Streamlit application
└── docs/                   architecture and operational notes
```

The root project directory is the Git boundary, but the live `brain/` vault is excluded from the code baseline because it contains owner-specific captures and memories. Back it up separately using an owner-controlled method. The existing local Git history still contains 18 old-layout vault files in its initial commit; there is no remote configured. Do not add a remote or push this history until those historical files have been reviewed or removed from history.

## Start with the model-free baseline

The Python package requires Python 3.11. From a source checkout, run
`setup.ps1` on Windows, or install `uv` and run `uv sync --locked
--no-default-groups` followed by `uv run --locked second-brain install`. This
creates the empty vault structure and a model-free SQLite lexical index. Then
run `uv run --locked second-brain doctor` to see which optional capabilities
are installed. The private `brain/` vault is excluded from this repository;
restore it from your own backup if you want existing memories.

On a Windows machine, run `setup.ps1`. It installs `uv` with WinGet when
available, falls back to user-scoped pip when Python exists, lets `uv` install
managed Python 3.11, syncs the locked dashboard dependencies, initializes
lexical recall, reports capabilities, and starts the local dashboard (use
`-NoLaunch` to stop after setup). It does not install the Ollama model server
or download LLM weights. macOS/Linux bootstrap scripts are not implemented yet.
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

The dashboard requires the optional `dashboard` extra. Install it with
`uv sync --locked --extra dashboard` before launch. Ollama must be installed
separately to enable local model-backed chat, embeddings, and capture. MCP,
Scout, multimodal capture, watchdog, and evaluation dependencies are separate
extras; none are required for offline lexical recall.

On first launch, the script prompts for a 12-character minimum owner password and confirmation. The browser session unlocks for up to 30 minutes of activity and can be locked from the sidebar. Reset it with `uv run --locked python scripts/dashboard_auth.py set-password`. The app is available at `http://127.0.0.1:8501`. The dashboard’s atom editor uses compare-and-swap, resets trust to pending after edits, and rebuilds lexical recall before reporting success. Approve only after inspecting the exact revision and its source excerpt.

This is a local single-owner gate, not protection from malware or another process running as the same OS user: that user can read the vault and can replace the application. Remote/proxy/tunnel use remains unsupported. Streamlit's native login is OIDC-only and requires an identity provider, so the offline dashboard uses its own salted `scrypt` verifier instead. CORS and XSRF are both enabled because Streamlit documents that XSRF alone does not protect WebSocket origin checks.

See [the dashboard security boundary](docs/dashboard-security.md) for the threat model, credential location, tested controls, and remaining cross-origin verification.

## Common commands

```powershell
uv run --locked python scripts/memory_review.py --status all
uv run --locked --extra online python scripts/review_atom.py approve <atom.md> --expected-sha256 <sha256> --actor <owner>
uv run --locked --extra online python scripts/review_atom.py merge <survivor.md> <superseded.md> --survivor-sha256 <sha256> --superseded-sha256 <sha256> --actor <owner> --rationale "<reason>"
uv run --locked python scripts/audit_sources.py
uv run --locked python scripts/rebuild_lexical_index.py
uv run --locked --extra evaluation python scripts/rebuild_vectors.py
uv run --locked python scripts/doctor.py
```

`review_atom.py approve` refuses missing or ambiguous evidence. Editing an atom later invalidates the approval, even if its frontmatter still says `accepted`. Rebuild dense vectors only when the embedder is available; the rebuild is staged and records its model, dimensions, generation, source revisions, and cosine metric. Chroma’s collection metric is configured explicitly because its default single-node metric is squared L2, not cosine ([Chroma collection configuration](https://docs.trychroma.com/docs/collections/configure)).

`review_atom.py merge` requires explicit owner-selected survivor/superseded atoms, both current revision hashes, an actor, and a rationale. It preserves both revisions in `.history`, marks the survivor pending for re-review, excludes the merged atom from normal recall, refreshes lexical recall, and writes paired decision records. If a dense index is active, the audit marks it stale; rebuild dense vectors before dense recall resumes. The dashboard does not yet expose this action.

`audit_sources.py` reports current source-anchor and approval state without changing atoms. After reviewing its report, explicitly demote stale approvals with `uv run --locked python scripts/audit_sources.py --demote-stale --actor <owner>`; re-approve only after inspecting the exact current source.

Dataset `second-brain-retrieval-pilot` v2.0.0 separates five candidate-ranking queries into two answerable cases, three exploratory pending-target cases, and two traps. The latest retrieval run used dense generation `ce48706db17d440fbba04b8c7f86c37e`; both answerable targets appeared in lexical and dense top-five and trusted context, while both traps returned zero trusted results. Median retrieval latency was about 6.6 ms lexical and 57.6 ms dense. Run `uv run --locked --extra evaluation python scripts/evaluate_answers.py --model qwen2.5-coder:7b --runs 3` to repeat end-to-end answer checks; the latest local run covered two answerable cases and two traps, with 6/6 positive claims and 0/6 trap leaks. Both datasets are tiny regression pilots, far too small to establish broad quality or a dense-retrieval advantage. Raw candidate labels are not approved memories. See the [system sweep](docs/system-sweep-2026-10-27.md) for model, dataset, latency, and hardware limits.

The answer run took 6.1 seconds p50 and 49.8 seconds p95 including cold start on this CPU-only host. The configured default `llama3.2:3b` did not answer either positive case in one repeat, so it is not yet a dependable default. These pilot results are too small to claim general model quality.

## MCP policy

MCP is not part of the default operating path. Do not connect this vault to cloud-connected hosts or expose the MCP process through a network service. The opt-in local interface returns only specifically requested, currently approved atoms, has no listable resource catalog, and writes bounded input to the untrusted raw inbox. Enabling MCP does not authenticate a client; only enable it for a trusted local process.

## Git safety

Use the project root as the single repository boundary. Review `git status` and the staged diff before committing. Do not use `git reset --hard` or `git clean` as rollback commands; restore only the specific files you intend to change, and preserve raw captures and atom history.
