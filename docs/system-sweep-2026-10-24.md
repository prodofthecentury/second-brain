# System sweep — 2026-10-24

## Verdict

The project has much stronger trust and data-loss controls than the earlier design, but it is not yet a functioning, trusted memory system in this environment. Its current recall path is safe because it returns **zero memories**: none of the 13 atoms has a current, verifiable owner approval. Dense recall is also unavailable because the installed Chroma native bindings fail to load. The lexical index is healthy, but it has no approved atoms to serve.

The best part of the system is now its refusal behavior: generated text cannot approve itself, edits demote an atom, and retrieval checks the reviewed revision and evidence. The weak part is operational usefulness: there is no approved memory to retrieve, no working dense generation, and no measured retrieval-quality baseline. Passing safety checks proves the guardrails exercised by those checks; it does not prove that the complete memory loop is useful or ready for unattended operation.

## Current observed state

`uv run scripts/doctor.py` reports:

- 13 atom files; zero currently approved revisions.
- A current local SQLite FTS index.
- A current Chroma cosine generation covers all 13 atom revisions (`0b3accb116fb45f691e29791ad093651`, `nomic-embed-text`, 768 dimensions). The Windows import failure was caused by the missing Microsoft Visual C++ v14 x64 runtime; the official signed runtime is now installed.
- Ollama is reachable and advertises four installed models. A real `nomic-embed-text` request returned 768 dimensions. The doctor checks model presence, not generation quality.
- The configured `llava` model is absent. A different model advertises vision capability, but vision remains disabled and unverified.
- 23 queued captures, 14 queued URLs, zero generated threads, and zero source Markdown pages.
- The dashboard is stopped after the runtime check.

## Findings by area

### Trust, evidence, and recall — materially improved

- Model output is normalized to candidate, pending, and missing citation state. Model-supplied trust fields and source paths are discarded or overwritten by system-controlled values.
- Approval checks the exact evidence excerpt against its named local source. Reused or ambiguous basenames fail closed. The current atom hash, latest owner decision, and source anchor must still match before recall can include it.
- Atom editing and review transitions go through one canonical mutator with compare-and-swap, a shared writer lock, an atom-history backup, a decision record, and lexical-index refresh. Edits reset the lifecycle to candidate/pending and remove approval anchors.
- If a valid cosine dense generation was active before a mutation, the mutator attempts to rebuild it while holding the shared lock. A failed dense refresh remains stale and unavailable. This behavior has a mocked regression check.
- Chroma cosine distance is converted across its full expected range rather than clamped at distance 1. New manifests declare cosine; legacy or non-cosine generations are refused.
- A real staged rebuild and cosine query now pass. The rebuild swaps the new generation into place and retains the previous directory as rollback. `scripts/chroma_rebuild_canary.py` verifies a real persistent collection can close, swap, reopen, query, and preserve the prior collection.

**Plain reading:** the trust boundary is now substantially better designed, but no approved atom means retrieval has not yet demonstrated practical value. Do not relax the gate just to make the interface look populated.

### Ingestion, generated content, and concurrency — safer

- The watchdog waits for file size and modification time to stabilize before dispatching ingestion.
- Refinery captures are hashed before drafting and rechecked under the shared writer lock before saving. New atoms and generated threads use exclusive creation, so collisions do not overwrite existing notes.
- External `.intake` items remain quarantined until owner promotion. Generated atoms point to a concrete archived capture or source page, not a potentially ambiguous basename.
- Multimodal ingestion also checks that its input did not change during model processing and creates new output without replacing a prior file.

The tests cover selected races, path ambiguity, rollback, and non-overwrite behavior. They are not a stress test of several live daemon/dashboard/MCP writers running at once.

### Network and local interfaces — constrained, with a residual Scout risk

- The shipped dashboard launcher binds to `127.0.0.1`; runtime verification found only a loopback listener, and the Streamlit health endpoint returned `ok`. The dashboard has no authentication. Keep it on the local machine; a proxy or tunnel changes the exposure model.
- MCP is disabled by default, has no client authentication, limits inbox writes to 100,000 characters, and no longer exposes list/read-all vault tools. Its policy correctly forbids cloud-connected clients. A trusted local MCP process still has access to sensitive local data.
- Scout now resolves all addresses once, rejects the URL if any answer is non-public, and routes the HTTP connection through a custom HTTPX/httpcore transport that connects only to those captured IPs while retaining the original hostname for the Host header and TLS verification. Redirects remain disabled, as are environment proxies. An HTTP server canary verifies that the URL hostname is never resolved during connection and that the Host header remains the requested hostname.

### Git and data preservation — one boundary, no reviewed baseline yet

- `git rev-parse --show-toplevel` and `git -C brain rev-parse --show-toplevel` both resolve to the project root. The nested `brain/.git` metadata was moved to the root; the old empty root Git metadata and a bundle backup were preserved under the system temp directory.
- The project ignores rebuildable `.sidecar`, `.chroma`, and `tmp/pdfs/`. The four PNGs in `tmp/pdfs/` are reproducible page previews rendered on 2026-07-27 from the source PDFs listed and hashed in `docs/learning-and-memory-design-synthesis.md`; they are retained on disk but excluded from Git. Canonical Markdown, raw captures, atom history, and source evidence remain in scope.
- **The working tree is not a reviewable baseline yet.** Git status still shows old vault-root paths as deleted and the new `brain/` tree plus project code as untracked. Nothing is staged or committed. The move did not delete those files from disk, but the current history does not yet represent the new layout. A reviewed baseline commit remains an owner action because the tracked content includes raw and private Markdown.
- `setup.ps1` previously ran `git init`, `git add .`, and `git commit` automatically. That was unsafe for a vault. It now only reports the detected boundary and explicitly avoids initializing, staging, or committing files.

### Documentation and operating model

- The root README now describes the local-only prototype, review gates, model-free lexical baseline, and current limitations.
- Older architecture drafts and the 2026-07-26 sweep remain in `docs/`; treat them as historical snapshots, not current operating instructions.
- Dependencies are still spread across `uv` script metadata and PowerShell launchers. Chroma requires the system Visual C++ runtime on this machine; document and verify that prerequisite on a clean Windows installation.

## Changes made during this sweep

- Consolidated atom edits and review decisions into the canonical mutator, added stale-approval and rollback checks, and made an active dense generation rebuild conditionally within the writer workflow.
- Tightened exact source resolution, system-owned provenance/source assignment, stable-file ingestion, and non-overwriting thread creation.
- Bound the dashboard to loopback, removed its remote font fetch, and kept raw/source/thread artifacts read-only in the dashboard explorer.
- Disabled broad MCP enumeration by default and documented local-only use.
- Corrected cosine manifest and score handling, made doctor report actual readiness, and rewrote the root README around the current local prototype.
- Established the project root as the single Git boundary, preserved the previous repository metadata, and removed automatic staging/commit behavior from setup.
- Installed the official Microsoft Visual C++ v14 x64 runtime after verifying its signature, enabling Chroma's Windows Rust bindings. Added an owner review of two representative atoms without creating owner decisions.
- Pinned Scout's network connection to the DNS answers validated by the application, and added an HTTP transport regression canary.
- Added a provisional retrieval evaluation corpus and runner. It separates candidate ranking from trusted recall, reports Recall@5/MRR/nDCG@5 and latency, and includes two unanswerable trap queries.

## Verification performed

- `uv run --python 3.11 --with httpx --with trafilatura --with beautifulsoup4 python scripts/system_safety_tests.py` — 14 focused regression tests passed, including DNS pinning.
- `uv run scripts/smoke_test.py` — `SMOKE_TEST_OK`.
- `uv run scripts/archive_manifest_canary.py` — `ARCHIVE_MANIFEST_CANARY_OK`.
- `uv run scripts/chroma_rebuild_canary.py` — `CHROMA_REBUILD_CANARY_OK` against real Chroma.
- `uv run scripts/rebuild_vectors.py` — rebuilt all 13 atoms into a staged Chroma generation; manifest verified current with cosine distance and 768-dimensional `nomic-embed-text` vectors.
- `uv run scripts/evaluate_retrieval.py --output docs/retrieval-evaluation-results-2026-10-27.json` — all 5 provisional positive queries retrieved every expected atom in top 5. Mean Recall@5 was 1.00 for both, lexical/dense MRR 0.90/1.00, and nDCG@5 0.939/1.00. Median latency was about 6.4/57.9 ms. Both trusted retrieval paths returned zero results on all seven queries, including both traps, because no atom has owner approval.
- `uv run scripts/doctor.py` — Chroma, the current vector manifest, FTS, Ollama, and both configured models reported OK; zero approved atoms and the stopped dashboard remained warnings.
- `docs/owner-review-sample-2026-10-27.md` — manually checked two atom excerpts against local sources. The structured-logging excerpt has an exact anchor but owner approval remains pending; the sodium-ion claim lacks its underlying article and was not approved.
- `python -m compileall -q scripts dashboard chat_with_memory.py` — passed.
- `uv run scripts/doctor.py` — exited successfully and reported the warnings listed above.
- Launched the shipped dashboard script; confirmed the only listener on port 8501 was `127.0.0.1`, and its health endpoint returned `ok`. Stopped it afterward and confirmed the port was free.
- Ran `setup.ps1`; it identified the project root and made no Git changes.
- `git diff --check` — passed. No changes are staged.

The retrieval numbers are a smoke-sized candidate-ranking result, not a release evaluation: five relevant queries are too few and their labels are explicitly provisional, not owner-curated. No claim that dense recall beats lexical recall is justified. There was no browser-level dashboard workflow check, long-running concurrency test, or MCP client integration test. An attempted external CLI review could not authenticate because its OAuth session had expired, so no independent external review was completed.

## Recommended next work, in order

1. Have the owner review the two sample audit findings and approve only evidence they personally endorse; then verify trusted retrieval returns exactly those revisions.
2. Replace the provisional retrieval labels with an owner-curated and frozen set, expand it across lookup/paraphrase/conflict/time-sensitive/trap cases, and rerun the evaluation before deciding whether dense retrieval earns its ~9x latency cost.
3. Perform a browser-level dashboard test for edit, approval, rejection, and stale-revision behavior.
4. Review the migrated Git status and decide whether canonical raw/private Markdown should enter the first baseline commit. `tmp/pdfs/` has been classified as reproducible derived previews and is ignored; files were not deleted.

## Bottom line

This is a guarded local prototype, not a dependable second brain yet. Dense recall now runs end to end and Scout's DNS target is pinned, but zero atoms are owner-approved, the evaluation labels still need owner review, and the Git migration has no reviewed baseline commit. Do not rely on it unattended until those owner-controlled steps are complete.
