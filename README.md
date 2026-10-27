# Second Brain

**Status:** Local-first prototype with explicit review and provenance gates. It is not a certified release. The vault contains durable Markdown; SQLite and vector stores are rebuildable indexes.

**Product goal:** a one-click installable Brain plugin with local/offline and online modes, plus adapters for owner-built AI hosts. Offline must be a first-class, state-of-the-art target, not a degraded fallback; online mode can add stronger models and other capabilities, with explicit controls over which context leaves the machine. Claims of outperforming every current product require head-to-head evaluation and are not established by this repository. Installation must profile each PC and report the capabilities that its hardware can actually support. This repository is not that product yet: it is a Windows-tested prototype with a CLI, Streamlit dashboard, local Ollama integration, and opt-in local MCP.

## Safety and readiness

- The dashboard binds to `127.0.0.1`. It has no user authentication, so use it only on the local machine and do not expose it through a proxy or tunnel.
- MCP is disabled unless `BRAIN_ENABLE_LOCAL_MCP=1` is explicitly set. Do not register it with cloud-connected clients. See [the current system sweep](docs/system-sweep-2026-10-27.md).
- Model-generated atoms are candidates. The model cannot grant owner approval or citation status. Approval checks an exact evidence excerpt against a named local source and records the reviewed atom hash.
- Recall returns only the current atom revision whose latest owner decision is approval and whose source evidence still verifies. One atom (`structured-logging.md`) is currently approved; the other 12 are not.
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

On a fresh code-only checkout, first run `.\setup.ps1` to create the empty
vault folders, then restore your private `brain/` vault from your own backup if
you want access to existing memories. This repository intentionally excludes
that live vault.

From PowerShell in the project directory:

```powershell
uv run scripts/rebuild_lexical_index.py
uv run scripts/doctor.py
```

The doctor treats a missing or stale lexical index as a failure. Missing Ollama models, dense indexing, and zero currently approved memories are reported as warnings because they affect optional capabilities or available content, not the lexical engine itself.

Launch the dashboard with:

```powershell
.\run_dashboard.ps1
```

It is available at `http://127.0.0.1:8501`. The dashboard’s atom editor uses compare-and-swap, resets trust to pending after edits, and rebuilds lexical recall before reporting success. Approve only after inspecting the exact revision and its source excerpt.

## Common commands

```powershell
uv run scripts/memory_review.py --status all
uv run scripts/review_atom.py approve <atom.md> --expected-sha256 <sha256> --actor <owner>
uv run scripts/rebuild_lexical_index.py
uv run scripts/rebuild_vectors.py
uv run scripts/doctor.py
```

`review_atom.py approve` refuses missing or ambiguous evidence. Editing an atom later invalidates the approval, even if its frontmatter still says `accepted`. Rebuild dense vectors only when the embedder is available; the rebuild is staged and records its model, dimensions, generation, source revisions, and cosine metric. Chroma’s collection metric is configured explicitly because its default single-node metric is squared L2, not cosine ([Chroma collection configuration](https://docs.trychroma.com/docs/collections/configure)).

The current local store has a verified dense generation and one current owner-approved atom. The seven-query evaluation set is owner-approved. Run `uv run scripts/evaluate_retrieval.py` to measure raw candidate ranking and the trust/relevance gate separately. On the current set, both trusted paths return zero results for both traps; only the structured-logging paraphrase retrieves approved context. Five positive queries over 13 atoms are too few to establish general relevance quality or show that dense recall earns its roughly 9x latency cost. Raw candidate rankings are not owner-approved memories.

## MCP policy

MCP is not part of the default operating path. Do not connect this vault to cloud-connected hosts or expose the MCP process through a network service. The opt-in local interface returns only specifically requested, currently approved atoms, has no listable resource catalog, and writes bounded input to the untrusted raw inbox. Enabling MCP does not authenticate a client; only enable it for a trusted local process.

## Git safety

Use the project root as the single repository boundary. Review `git status` and the staged diff before committing. Do not use `git reset --hard` or `git clean` as rollback commands; restore only the specific files you intend to change, and preserve raw captures and atom history.
