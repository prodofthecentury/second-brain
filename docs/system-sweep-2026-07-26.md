# System Sweep — 2026-07-26

**Status:** Not releasable as a trusted second brain. This is a diagnostic and release-gate record; no queued ingestion, Scout run, or night shift was started.

## Checks that passed

- `uv run scripts/smoke_test.py` → `SMOKE_TEST_OK`.
- `uv run python -m compileall -q scripts dashboard chat_with_memory.py` → success.
- `uv run scripts/memory_review.py --status all --json` → all 13 atom files parsed.
- `uv run scripts/doctor.py --json` after starting the installed Ollama server → exit code 0; Ollama, `llama3.2:3b`, and `nomic-embed-text` are available.
- Direct local API probes:
  - `llama3.2:3b` responded exactly `HEALTHY`.
  - `nomic-embed-text` produced one 768-dimensional embedding.
- A controlled dashboard command pinned to Python 3.11 bound port 8501.

## Runtime facts

- 16 GB RAM; Intel i7-6700HQ, 4 cores/8 logical processors; Nvidia GTX 965M with 2 GB VRAM.
- Installed Ollama models: `llama3.2:3b`, `nomic-embed-text`, `qwen2.5-coder:7b`, and `qwen3.6:latest` (23 GB).
- ChromaDB persistent mode is unavailable, so the JSON vector fallback is active.
- 23 raw captures and 14 URLs are queued.
- The project directory is not currently a Git repository.

## Required repairs before a trusted release

1. **Critical — remove silent hash embeddings.** The current 256-dimensional fallback can coexist with real 768-dimensional vectors in the active JSON store; mismatches score `0.0` and can return arbitrary results. Store model, dimension, and source hash per vector; fail closed if embedding is unavailable.
2. **Critical — enforce trust in code.** All parsed atoms resolve to `pending`; default retrieval includes `pending` and `needs_edit`, while answer prompts only say to prioritize the vault. Add the approved trust/citation state machine and structural answer policy.
3. **Critical — isolate web/MCP input.** Scout accepts arbitrary redirected URLs and feeds their content to the model; MCP can enumerate the vault and write unbounded inbox content. Add SSRF controls, quarantine, size limits, explicit local consent, and no default enumeration.
4. **Critical — eliminate destructive thread regeneration and concurrent writers.** Editor overwrites thread files; daemon, dashboard, and scripts can mutate the same files/index concurrently. Add the canonical single-writer protocol and separate derived views from human notes.
5. **Critical — repair dashboard startup.** `run_dashboard.ps1` invokes `uv` without pinning the required Python version. It selected Python 3.14.6 and did not bind 8501. The controlled `--python 3.11` launch worked; the launcher itself remains unfixed.
6. **Required — use content-hash incremental indexing.** Do not re-embed every atom on every run. A rebuild must fail loud rather than silently skip malformed atoms or write fallback vectors.
7. **Required — add schema versioning and migration.** Existing atoms lack explicit review status and use unvalidated contexts; missing fields must be reported as legacy, not disguised as trustworthy defaults.
8. **Required — initialize an owner-approved backup strategy.** The current `.gitignore` does not exclude derived vector state or web-source content, and there is no local repository. Do not initialize Git or alter retention without owner approval.

## Mandatory red-team re-verification

After each repair, execute the relevant test and failure injection:

| Area | Pass condition | Block release when |
| --- | --- | --- |
| Embeddings | Served generation has exactly one real model/dimension; source hashes match. | embedder outage writes a substitute vector; a foreign vector is scored instead of refused. |
| Atom transitions | Crash leaves an old-or-new valid file; an external edit causes compare-and-swap refusal. | a torn or silently clobbered atom exists. |
| Citation integrity | Quote, offsets, and source hash validate; an edit demotes the atom. | an edited/reviewed atom retains stale trust. |
| Scout/MCP | private, loopback, link-local, redirected private, and oversized input are refused; no remote item auto-promotes. | any SSRF, full-vault enumeration, or auto-promotion succeeds. |
| Threads | Human text is never overwritten by a derived run. | two runs or one editor run loses human content. |
| Retrieval | Per-category score meets baseline within the laptop SLO; trap-query fabrication rate is zero. | any threshold regresses or a trap is asserted as fact. |
| Runtime | The shipped dashboard launcher binds port 8501 on a clean startup; capability probe matches observed model behavior. | launcher fails, or an absent capability remains callable. |

## Safe current operating posture

The local Ollama server may run for direct, manual small-model use. Do not register the MCP server with cloud clients, run the Web Scout, start the daemon, run the night shift, or rebuild the active vector store until the critical repairs are complete and re-verified.

