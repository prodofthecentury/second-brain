# System sweep — 2026-10-27

## Verdict

The project has working, source-backed owner review; a real local Chroma cosine index; public-IP-pinned Scout fetches; and a local dashboard launcher that starts on loopback. It is still not a release-ready factual assistant. A conservative two-term overlap gate blocks the two owner-personal trap queries, while a fresh audit caught that the gate was also suppressing the approved logging paraphrase because it ignored the atom's supporting Evidence section. The gate now checks both Claim and Evidence. That remains a literal heuristic, not a proof of answerability.

## Current state

- 13 canonical atoms; one current owner-approved atom (`structured-logging.md`). The sodium-ion lead remains unapproved because the underlying article is absent and its raw note is explicitly tentative.
- Chroma generation `231a0548c2a847b6bd7ecaf6a907756b`, `nomic-embed-text`, 768 dimensions, cosine metric; current FTS index. Windows Chroma imports after installing the official signed Microsoft Visual C++ v14 x64 runtime.
- Ollama exposes four installed models; `llama3.2:3b` and `nomic-embed-text` are configured. `llava` is absent; another advertised vision model remains unverified and disabled.
- 23 raw captures, 14 queued URLs, no source Markdown pages or generated threads.
- `run_dashboard.ps1` returned HTTP 200/`ok` from `/_stcore/health`; its listener was `127.0.0.1:8501`. It was stopped after verification. The dashboard has no authentication.
- The code-only baseline is committed as `f4ab16e` (`chore: establish code-only project baseline`). The working tree is clean; the live `brain/` vault and `tmp/` remain local and ignored. There is no Git remote. The prior commit `bffca9e` still contains 18 old-layout vault files in local history, so do not add a remote or push until that history is reviewed or scrubbed.

## Retrieval result and change

Owner-approved evaluation corpus: five positive queries and two no-factual-answer traps. Raw candidate ranking remains unchanged by the trust relevance filter:

| Metric | Lexical | Dense |
| --- | ---: | ---: |
| Mean Recall@5 | 1.00 | 1.00 |
| Mean MRR | 0.90 | 1.00 |
| Mean nDCG@5 | 0.939 | 1.00 |
| Median retrieval latency | 5.8 ms | 59.1 ms |

The raw ranking metrics are over only five positive queries and 13 atoms. They do not show that dense retrieval is better in representative use, and the ~9x median latency difference does not include p95 or end-to-end answer latency.

Before the relevance check, both trusted paths returned one approved result for each trap (four false-positive retrievals total). The test-first change requires at least two meaningful query tokens to occur in a candidate's `Claim` or `Evidence` section before lexical or dense recall can return it; linked neighbors receive the same check. The fresh live rerun returned **zero trusted results for both traps** and returned the currently approved `structured-logging.md` atom for the structured-logging paraphrase through both retrieval paths. The other positive examples remain unapproved and are correctly withheld from trusted context. Candidate ranking must not make pending atoms answerable.

The overlap test is a conservative literal match. It can miss valid paraphrases and it is not proof that the returned atom answers the question. Do not describe the current 0/2 trap result as a zero fabrication rate: `evaluate_retrieval.py` counts retrieved documents, not generated answers. The CLI and dashboard now structurally abstain without calling the model when retrieval yields no approved context. With context present, the model may still overstate or invent claims, so answer-level grounding is still unmeasured.

The current evaluator has no separately labelled dense-only, keyword-only, and combined-ranker categories; it measures lexical and dense candidate rankings independently. The default `search_memory` selects lexical when the FTS sidecar exists. The owner-approved set is a useful initial regression baseline, not calibration evidence for score weights or general answerability.

## Other security and integrity findings

- **Review and provenance:** approval requires an exact quote in a uniquely resolved local source, stores source hash/byte offsets, and is tied to the exact atom revision and latest owner decision. Current approval was explicitly authorized by the owner. Source anchoring is not independent proof that the local captured guidance is authoritative.
- **Scout:** validates every DNS answer, pins the HTTP connection to those addresses, rejects redirects, disables environment proxies, and caps response size. External captures remain `web_remote` and quarantined. The canary test exercises a real local server through the pinned transport.
- **MCP:** disabled unless explicitly enabled; no listable resource catalog; approved-only atom reads; bounded untrusted inbox writes. It has no client authentication. Do not attach a cloud-connected client.
- **Dashboard:** loopback binding is verified, not authenticated access. Its safety depends on keeping it local and not placing it behind a proxy/tunnel.
- **Dependencies:** there is no `pyproject.toml` or `uv.lock`; the launcher resolves and installed 44 packages at runtime. It worked on this machine, but dependency versions and fresh/offline setup are not reproducibly pinned.
- **`tmp/`:** inventory contains exactly four PNG page previews under `tmp/pdfs/`, rendered from the PDFs listed in the learning-design memo. They are reproducible derived previews, retained on disk and ignored by `.gitignore`. No other files were found under `tmp/`.
- **Independent reviewer:** the required read-only Claude Opus review was attempted using the saved session, but failed because the CLI OAuth session had expired and could not be refreshed. No independent review is claimed.

## Verification performed

- `uv run --python 3.11 scripts/smoke_test.py` — passed after adding lexical/dense evidence-supported paraphrase checks, no-context abstention checks, and trap regression assertions. The new paraphrase regression failed before the Claim+Evidence implementation.
- `uv run --python 3.11 scripts/evaluate_retrieval.py --output docs/retrieval-evaluation-results-2026-10-27.json` — passed against the live Chroma generation; raw ranking metrics above; the approved structured-logging paraphrase retrieves one distinct trusted atom through each path; trusted trap false positives `0`.
- `uv run --python 3.11 scripts/chroma_rebuild_canary.py` — `CHROMA_REBUILD_CANARY_OK` for both canary atoms.
- `uv run --python 3.11 --with httpx --with trafilatura --with beautifulsoup4 python scripts/system_safety_tests.py` — 14 tests passed after the relevance change.
- `uv run scripts/archive_manifest_canary.py` — passed after the relevance change.
- `uv run scripts/chroma_rebuild_canary.py` — passed against persistent Chroma after the relevance change.
- `uv run python -m compileall -q scripts dashboard chat_with_memory.py` — passed. The full staged baseline check reports 129 trailing-whitespace lines and three extra blank EOFs in existing project sources/docs; no whitespace cleanup was applied because some spaces occur in prompt and UI text.
- Shipped dashboard launcher — started successfully; health endpoint returned `ok`; only loopback listener observed; stopped and confirmed no listener remains.
- `uv run scripts/doctor.py` — current indexes and one approved atom reported OK; vision is unverified/unavailable and dashboard is stopped after the launcher check.

## Still open

1. Add more owner-approved memories only after source review; keep sodium-ion pending until primary evidence is present.
2. Expand the gold set and manually inspect missed matches, false matches, and answer output. Add explicit dense-only/keyword-only/hybrid categories and p95 latency before making a dense/default decision.
3. Evaluate answer-level factuality on repeated model outputs and broader trap questions. Empty-context abstention is now structural in the CLI and dashboard, but that does not prove grounded answers when context is present.
4. Before any future remote is configured, review or remove the 18 old-layout vault files retained in local Git history. The current code-only baseline is local and clean; the live vault remains excluded.
5. Obtain the required independent Claude review after its CLI authentication is restored, then reconcile its findings.

## Product direction

The owner clarified the long-term goal: a one-click installable Brain plugin
for any PC, with local/offline mode and online mode. Offline must be a
first-class, state-of-the-art target, not a weak fallback. Online mode may offer
stronger models and additional capabilities; it must have explicit controls
over which data is sent to which host. Owner-built AI hosts are also in scope.
The current repository is not close to meeting that distribution claim. It has
no packaged plugin contract, host-adapter API, supported-host matrix, portable
installer, or cross-platform verification. The safe direction is a
host-independent memory core plus narrowly scoped adapters. Keep the current
local runtime and trust gates as the first core proving ground while designing
the online tier's data-sharing and permission boundaries. “Better than anything
available today” remains an unproven evaluation target; define representative
tasks and compare against current alternatives before making that claim. A
one-click installer must probe each PC and expose a truthful capability profile
because hardware cannot provide identical model quality everywhere.

## Commands and operator notes

Rebuild and inspect indexes with `uv run scripts/rebuild_lexical_index.py`, `uv run scripts/rebuild_vectors.py`, and `uv run scripts/doctor.py`. The vector rebuild requires the configured local embedder and the Windows Visual C++ runtime for native Chroma. Keep external ingestion, MCP, and unattended workflows off until their remaining release gates are met.
