# Codex CLI Handoff — Second Brain

**Last refreshed:** 2026-11-15 (Pacific/Auckland)

**2026-11-15 Milestone Completion Note:** Goals 0–16 (`tasks/plan.md`, `tasks/todo.md`) are now implemented, adversarially audited across Phases 1, 2, 3A, and 4, and verified at `10/10 PASS` in `scripts/test_all.py` (see `docs/system-sweep-2026-11-15.md`, `docs/phase2-architecture-and-sweep-2026-11-15.md`, and `docs/phase3a-and-phase4-architecture-2026-11-15.md`). Both `master` and the history-scrubbed `release-code-only` branch are synchronized.

**Current runtime follow-up (historical):** 2026-10-27. Native Chroma imports and queries
the current 14-atom cosine generation after installing the official signed
Microsoft Visual C++ v14 x64 runtime. Scout pins connections to validated
public DNS results. The owner approved `structured-logging.md` and
`design-for-failure.md`, plus the seven-query evaluation set. The corrected
procedure candidate and product-target draft remain pending. The
Claim+Evidence literal overlap gate keeps both current traps out while
returning approved logging and failure-handling memories through lexical and
dense paths. CLI and dashboard abstain without approved context; answer
grounding with context is still unmeasured. Current test/evaluation
evidence and open gaps are in `system-sweep-2026-10-27.md`.

The retrieval dataset is now version 2.0.0: five candidate-ranking examples,
two answerability examples pinned to approved atom revisions, three exploratory
examples, and two documented abstention traps. The evaluator rejects stale or
unapproved answer targets before scoring. All cases are still regression
examples; there is no development or holdout set, and generated answer quality
has not been evaluated.

The owner-selected code-only baseline is committed through `db9d182`; its
checkpoint was clean, and the live `brain/` vault and `tmp/` are excluded. The
roadmap/review preparation is currently uncommitted. No remote is configured.
The earlier `bffca9e` commit still contains 18 old-layout vault files, so do
not push until that history is reviewed or scrubbed. The current dependency-
ordered roadmap is `tasks/plan.md` and `tasks/todo.md`; they supersede the old
checklist chronology below. The 2026-10-24 report is a historical snapshot.

**Long-term product direction:** the owner wants a one-click installable Brain
plugin for any PC, with local/offline and online modes and adapters for
owner-built AI hosts. Offline must be a first-class, state-of-the-art target;
online mode may offer stronger models and more features, with explicit
per-host data-sharing controls. “Better than anything available today” is an
evaluation target, not a proven claim. The installer must report machine-specific
capabilities. The current local prototype does not yet meet this packaging or
compatibility goal.

**Workspace:** `C:\Users\camer\.gemini\antigravity\antigravity_projects\projects\second_brain`

**Use this document:** Read it completely before implementing. It preserves the prior multi-model architecture work and system-sweep evidence. Treat current files and commands as authoritative when they conflict with this document.

## Immediate orientation

The original project is a Python/Ollama/Markdown second brain. It already contains capture, multimodal ingestion, a vector store, Graph-RAG-like retrieval, terminal chat, a Streamlit dashboard, Web Scout, watchdog daemon, and MCP server. Its feature breadth is ahead of its trust, integrity, privacy, concurrency, and recovery foundations.

The safe goal is **not** “add more agents.” Build a reliable memory substrate first, then let advanced local models consume it. The approved direction is detailed in [architecture-review-draft.md](architecture-review-draft.md); the baseline release blockers are in [system-sweep-2026-07-26.md](system-sweep-2026-07-26.md).

The copy-paste task instruction for a new Codex CLI session is at [../CODEX_CLI_GOAL.md](../CODEX_CLI_GOAL.md).

## User intent that must be preserved

The owner asked for a genuinely advanced AI with a second brain: smart, helpful, efficient, fast-learning in the safe sense of improving its *reviewed memory*, and deeply reasoned rather than generic. On 2026-10-27 they clarified the product target: a one-click plugin that works with local/offline and online AI hosts, including owner-built systems; online mode should provide richer capabilities. Preserve explicit data-sharing boundaries when using online hosts. They also required sustained back-and-forth with the strongest Claude model, multiple architecture passes, repeated checks, a complete self-sweep, an independent Claude sweep, and a final self-audit of Claude’s conclusions. Do not fake duration, but do not deliver a shallow outline or pretend passing smoke tests proves trust.

The owner wants to read the full work before an unsupported “done” claim. Preserve evidence in workspace documentation.

### Learning-model boundary

The supplied source study confirms a central design boundary: the project improves through bounded context assembly, immutable evidence, reviewed memory, deterministic derived indexes, and owner-approved workflow changes. It does **not** perform continual fine-tuning, PPO/RLHF, self-generated reward optimisation, or autonomous parameter updates. The current laptop is not treated as capable of those activities merely because a model is installed. See [learning-and-memory-design-synthesis.md](learning-and-memory-design-synthesis.md) for source hashes, analysis, source limitations, and the proposed cognitive architecture.

## What already happened

### Cross-model review record

Claude Code CLI was available at `C:\Users\camer\.local\bin\claude.exe`, version `2.1.211`. Claude Opus was invoked with maximum effort in a read-only session. The session ID was:

`4af778e0-84c5-4e82-bda6-510a173a69de`

The exact review prompts are stored under `tasks/`:

- `claude-architecture-pass-1.md`
- `claude-architecture-pass-1-continue.md`
- `claude-architecture-pass-2.md`
- `claude-architecture-pass-3.md`
- `claude-system-sweep.md`

The safe read-only invocation used was:

```powershell
Get-Content -Raw tasks\claude-architecture-pass-3.md | claude -p "" --resume "4af778e0-84c5-4e82-bda6-510a173a69de" --model opus --effort max --permission-mode plan --tools "Read,Glob,Grep" --safe-mode --output-format text
```

For new review artifacts, write the prompt to a file and pipe it to Claude; do not interpolate source artifacts into a shell-quoted command. Keep tools read-only unless the owner grants broader authority.

### Pass 1 — current-system critique

Claude found the central contradiction: the project claims integrity but optimizes for availability through silent fallbacks.

Key verified findings:

1. `scripts/ollama_utils.py:embedding_with_fallback` catches all embedding errors and returns a 256-dimensional hash embedding. The real `nomic-embed-text` model returns 768 dimensions. `scripts/vector_store.py:cosine_similarity` returns `0.0` when dimensions differ. The JSON vector backend accepts both, then ranks all mismatch scores arbitrarily. This can silently poison recall.
2. `scripts/ollama_refinery.py:embed_existing_atoms` re-embeds every atom on every run; an embedder outage can replace a healthy index with hash vectors.
3. `scripts/recall.py:NORMAL_RECALL_STATUSES` includes `pending` and `needs_edit`. Chat/dashboard prompts tell the model to prioritize vault context without enforcing that pending material is less trustworthy.
4. Every current atom resolves to `pending`: existing atom files have no explicit `review_status`; the review parser supplies the default. Existing `contexts` values also mix topics with environment/context tags.
5. Confidence/importance scores are often constant and model-generated; one real atom sets `certainty: established` despite the house rules. Model-set certainty cannot be used as a trust signal.
6. `scripts/ollama_scout.py:fetch_article` fetches arbitrary redirecting URLs without SSRF protections, then passes extracted text into the refinery prompt. This permits internal-network fetches and indirect prompt injection.
7. `scripts/mcp_server.py` exposes vault enumeration and unbounded `add_to_inbox` writes without scope, consent, redaction, or rate/size limits. README guidance to attach it to cloud hosts contradicts privacy.
8. `scripts/ollama_editor.py:write_thread` opens thread files with `"w"`; normal editor runs can overwrite human edits.
9. “Graph RAG” is only one-hop wikilink expansion with no real typed/lifecycle semantics. Do not market it as advanced graph intelligence before it earns that claim.

### Pass 2 — adversarial correction to the initial proposal

The initial five-plane architecture was rejected as too elaborate. Claude’s correction:

- Reduce to **one immutable evidence store, one canonical Markdown store, and one derived read model**.
- SQLite is allowed only for a rebuildable FTS/vector/telemetry projection. It must not hold authoritative review state, locks, or the only copy of the queue.
- Do not add a SQLite lock table. A single mutator uses an OS singleton/lockfile; SQLite’s serialisation is only a backstop, not business logic.
- Do not use six model-scored trust fields. Keep machine-stamped provenance, deterministic citation checks, and only owner-controlled behavior gates.
- Citation anchors must use immutable byte/normalized-character offsets into a hash-verified source, not chunk indices that change when chunking changes.
- A quote existing at an offset proves only citation integrity, not claim entailment.
- On current hardware, model-free lexical FTS is the baseline. Dense retrieval must earn itself by evaluation. Cross-encoder reranking, multi-hop semantic expansion, real-time per-file extraction, vision, automatic nightly synthesis, and 7B+ default models are absent, not merely “optional.”
- Do not implement a full event ledger in v1. Atomic per-atom frontmatter replacement plus backups and a flat decision record are sufficient until a genuine multi-atom atomic operation is needed.

### Pass 3 — final architecture flaw found and resolved

The key flaw in the reconciled design was allowing human Markdown edits to merely rebuild indexes. That would leave `human_reviewed` or `source_anchored` state on changed text that was never rechecked.

Final rule: **a human edit to an existing atom is a canonical event.** It must use compare-and-swap, re-run citation integrity, append a human-edit/decision record, and demote trust state to `candidate` until it is re-anchored and (where applicable) re-affirmed by the owner.

The final first-release slices are:

1. Truthful runtime baseline / fail-closed embeddings.
2. Immutable evidence and sticky provenance manifests.
3. Lexical FTS behind the shared recall contract.
4. Citation-integrity engine plus claim state and edit-triggered demotion.
5. Safe owner review transitions with atomic replacement and single writer.
6. Structural Standard/Research answer assembly and trap-query tests.
7. Quarantined Scout/MCP ingestion plus explicit bounded drafting.

### Pass 4 — independent Claude system sweep

Claude’s verdict was **not releasable**. It differentiated passing smoke/compile checks from actual trust readiness. It confirmed these Critical issues remain unimplemented:

- C1: silent hash fallback / mixed-dimension JSON index.
- C2: pending/needs-edit material is normal answer context.
- C3: no citation-integrity or entailment gate.
- C4: unrestricted Scout input reaches model/vault.
- C5: MCP full-vault enumeration and unbounded write.
- C6: editor overwrites threads.
- C7: daemon/dashboard/scripts race atom and JSON-store mutations.
- C8: shipped dashboard launcher starts under wrong Python.

Required but secondary issues: re-embed-everything performance; legacy schema drift; rebuild silently skipping/poisoning; missing local Git recovery baseline; model-set certainty failure.

The primary agent re-audited Claude’s claims against source. Correction: the workspace is **not a Git repository at all**, not simply a repo with empty history. The rest of the finding set is supported by current code/runtime evidence.

## Current runtime and hardware facts

Measured on 2026-07-27:

- Windows laptop with 16 GB RAM, Intel i7-6700HQ (4 cores, 8 logical processors), Nvidia GTX 965M (2 GB VRAM), reported driver `24.21.13.9901`.
- Ollama CLI is installed. The service was started during prior verification and may be running; check it rather than assuming.
- `uv run scripts/doctor.py --json` currently exits `0` but warns that Chroma persistent mode is unavailable, vision model `llava` is absent, and dashboard port 8501 is not listening.
- `ollama list` reports:
  - `llama3.2:3b` — 2.0 GB
  - `nomic-embed-text:latest` — 274 MB
  - `qwen2.5-coder:7b` — 4.7 GB
  - `qwen3.6:latest` — 23 GB
- Direct local API probe previously verified that `llama3.2:3b` returned exactly `HEALTHY` and `nomic-embed-text` returned a 768-dimensional vector.
- A listed model is not a proven viable capability. In particular, do not assume the 23 GB Qwen model is usable on 16 GB RAM.
- Current data counts: 14 atoms, 0 threads, 0 source files, 24 queued captures, and 14 queued URLs.
- Do not start Scout, daemon, night shift, MCP, or vector rebuild before the corresponding safety slices. The queued data is user data and intentionally untouched.

## Dashboard launcher verification

`dashboard/app.py` declares `requires-python = ">=3.11,<3.12"`. The shipped launcher is pinned to that supported Python version:

```powershell
uv run --python 3.11 --with streamlit --with plotly --with networkx --with pandas --with chromadb --with ollama streamlit run dashboard/app.py
```

The project-local `.streamlit/config.toml` also sets `server.headless = true` so startup does not block on Streamlit's first-run email prompt, and `browser.gatherUsageStats = false` to opt out of usage telemetry. On 2026-07-27, a clean launcher run returned HTTP 200 with body `ok` from `http://127.0.0.1:8501/_stcore/health`, then was stopped without leaving a listener. This verifies the shipped startup path; `doctor.py` will still correctly report the dashboard as unreachable unless it is currently running.

## Existing layout

```text
brain/
  0-raw/               Raw capture inbox; 23 files currently queued
  1-desk/              Scratch/handoff area
  2-atoms/             13 Markdown memory atoms
  3-threads/           Generated synthesis threads; currently empty
  sources/             Scraped source content; currently empty
  house-rules.md        Current local-model instructions
  .chroma/              Current derived vector store
scripts/
  config.py             Paths and model defaults
  memory_format.py      Markdown/frontmatter formatting and normalization
  memory_review.py      Current listing-only review queue
  ollama_utils.py       Model calls and dangerous fallback embedding
  vector_store.py       Chroma-or-JSON backend
  recall.py             Shared search/ranking/graph-neighbor contract
  ollama_refinery.py    Drafting, critic, archive, atom writes, embedding
  ollama_editor.py      Synthesis thread generation (currently destructive)
  ollama_scout.py       Web crawler (currently unsafe)
  mcp_server.py         MCP interface (currently unsafe)
  watchdog_daemon.py    Current real-time mutation path
  rebuild_vectors.py    Current unsafe rebuild path
  doctor.py             Runtime health check
  smoke_test.py         Existing tests; insufficient for trust guarantees
dashboard/app.py        Streamlit dashboard
chat_with_memory.py     CLI chat
run_dashboard.ps1       Broken launcher
run_daemon.ps1          Watchdog launch script
run_night_shift.ps1     Refinery then editor; do not run yet
```

## Documentation and task status

- `docs/unified-local-ai-brain.md` and `docs/cognitive-core.md` are prior project/spec material. They are useful background but predate the final cross-model review.
- `docs/architecture-review-draft.md` is the current proposed architecture and supersedes conflicting direction in older task documents.
- `docs/system-sweep-2026-07-26.md` is the current pre-repair release gate.
- `tasks/plan.md` and `tasks/todo.md` were reconciled on 2026-10-27 against the current source and system sweep. Follow their Goal 0–8 dependency order; do not revive the historical unchecked tasks below.
- `tasks/claude-*.md` contain the exact prior external-review prompts. Outputs were reconciled into this handoff and the two review documents.
- `CODEX_CLI_GOAL.md` is the ready-to-copy task instruction.

## Non-negotiable target contract

### Data boundaries

1. **Immutable evidence plane:** original bytes, SHA-256, origin, custody events, source locations, and offsets. Never overwrite evidence. Capture may append a new file while models are down.
2. **Canonical Markdown plane:** atom text and current human-readable state. Existing-atom edits/transition happen through one canonical mutator using atomic temp-write-and-replace plus compare-and-swap against the read hash.
3. **Derived sidecar:** SQLite FTS5/vector catalog/telemetry only. It can be deleted and rebuilt without losing source evidence or an owner decision.

### Trust facts and state

- `origin`: `user_local`, `mcp_local`, `web_remote`, or `imported_unknown`; system-stamped and cannot be silently cleared.
- `citation_integrity`: `anchored`, `missing`, `hash_mismatch`, `invalid_offset`; checks source bytes, hash, quote, and offset.
- `claim_state`: `candidate`, `source_anchored`, `human_reviewed`, `superseded`, `rejected`, `quarantined`.
- `epistemic_label`: owner-selected `observation`, `preference`, `hypothesis`, `claim`, `procedure`.

Only the owner decides entailment/review. An anchored citation does not mean the claim is true. Hypotheses/preferences must not be presented as facts merely because their source is intact.

### Answer policy

- Standard answers: may state `human_reviewed` material directly. `source_anchored` material must be model-external, labelled evidence chrome, not unmarked model prose. Candidate and quarantined content cannot become factual assertions.
- Research answers: may display candidate/quarantined material only as labelled leads.
- Multi-atom synthesis does not gain factual status automatically. New synthesized claims need their own evidence/review gate.

### Model/index policy

- No fake/hash fallback vector in a served index.
- Stored vectors have model, dimension, content hash, and index generation.
- Query/refinement rejects foreign model/dimension/generation rather than scoring `0.0`.
- Index upgrade: build separate generation, audit/evaluate, atomically flip active generation, retain prior only until cutover verified.
- Dense retrieval must beat lexical FTS baseline on owner-labelled retrieval evaluation and meet measured p95 latency on this actual machine before it becomes the default.

### External and concurrency policy

- Scout/MCP input receives sticky external origin and quarantine. Defend against schemes, private/loopback/link-local/metadata addresses, redirects, oversized payloads, prompt injection, and automatic promotion.
- MCP is local/consented/read-only by default. No full-vault enumeration. No non-local host unless owner grants direct approval.
- Existing atom mutations and derived index writes use exactly one OS-enforced mutator. New raw capture files can be append-only and collision-resistant.
- Derived threads are either machine-only views written separately or human notes that the machine never overwrites. Never both.

## Mandatory TDD / sweep cases

Write behavioral tests first. The existing `smoke_test.py` currently checks some source strings and does not prove safety.

1. Embedder outage during refinery/rebuild writes zero substitute vectors and leaves the index generation incomplete/degraded.
2. Seeded foreign-dimension vector is refused/quarantined, never scored or returned.
3. Standard-mode trap query whose only “answer” is candidate/quarantined returns no factual assertion.
4. Editing a reviewed/anchored atom body invalidates citation integrity and demotes its claim state.
5. Stop/restart or simulated crash mid-transition yields either old or new valid atom, never partial file.
6. Concurrent external edit makes compare-and-swap fail safely rather than clobbering content.
7. SSRF inputs (`127.0.0.1`, `[::1]`, `169.254.169.254`, private redirects, oversized payloads) are refused.
8. MCP cannot enumerate the vault or write/prompt-promote content without scope/approval.
9. Human thread content survives repeated machine editor runs.
10. Shipped `run_dashboard.ps1` binds port 8501 within a defined bound on Python 3.11.

Then run static compilation, focused tests, full smoke suite, doctor, integrity audit, retrieval evaluation (Recall@k/MRR/nDCG by category), trap-query fabrication test, and p50/p95 latency on this laptop. Claude Opus must independently review the source/test evidence, then the primary agent must audit Claude’s findings one more time.

## Commands used successfully

```powershell
# Health / basic code checks
uv run scripts/doctor.py --json
uv run scripts/smoke_test.py
uv run python -m compileall -q scripts dashboard chat_with_memory.py
uv run scripts/memory_review.py --status all --json

# Runtime facts
ollama list
ollama ps

# Direct local generation/embedding probe (no download)
$generateBody = @{model='llama3.2:3b'; prompt='Reply with exactly HEALTHY.'; stream=$false; options=@{num_predict=3}} | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post -Uri 'http://localhost:11434/api/generate' -ContentType 'application/json' -Body $generateBody
$embedBody = @{model='nomic-embed-text'; input='second brain diagnostic'} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:11434/api/embed' -ContentType 'application/json' -Body $embedBody
```

## Safety notes and outstanding owner decisions

- The project root is now the sole Git worktree. The existing `brain/.git` metadata was moved to the root, preserving its history; the former empty root `.git` and a history bundle are kept under the system temp directory as reversible backups. No files were staged or committed. Review the whole current vault and choose explicitly before committing raw/private Markdown. `brain/.sidecar/` and `brain/.chroma/` are excluded as rebuildable state.
- Do not download models or assume the 23 GB Qwen model is usable. Model installation/upgrades are owner decisions.
- Do not connect Claude Desktop, Cursor, or any cloud client to MCP. The owner asked for Claude review through the installed CLI; that did not authorize exposing the vault via MCP.
- Do not delete/rewrite current atom/raw files during migration. Make additive manifests/sidecars first and prove reconstruction.
- The first work slice should be the fail-closed index/runtime baseline and dashboard launcher regression, not an unrelated dashboard redesign.

## Current completion status

Architecture, local pilot, Phase 2 universal plugin/API, Phase 3A Cognitive Core (`skill_playbooks.py`, `executive_planner.py`), and Phase 4 scale/notes/product surfaces (`index_manifest.py`, `lexical_store.py`, `notes_store.py`, `streaming_chat.py`, `bulk_import.py`, `api_product.py`, `ide_connector.py`, `desktop/`, `extensions/vscode-second-brain/`, `plugins/`) are implemented, adversarially audited across Phases 1, 2, 3A, and 4, and verified across all Goals 0–16 (`tasks/plan.md`, `tasks/todo.md`). See [system-sweep-2026-11-15.md](system-sweep-2026-11-15.md), [phase2-architecture-and-sweep-2026-11-15.md](phase2-architecture-and-sweep-2026-11-15.md), and [phase3a-and-phase4-architecture-2026-11-15.md](phase3a-and-phase4-architecture-2026-11-15.md) for full details and audit reconciliations.

The unified release gate (`scripts/test_all.py`) passes all **10/10** suites (`doctor`, `evaluation_dataset`, `audit_sources`, `system_safety_tests`, `phase2_safety_tests`, `phase3_cognitive_tests`, `phase4_product_tests` with 25 tests, `scale_benchmark_1500`, `chroma_rebuild_canary`, and `smoke_test`). Historical findings above describe earlier audit states; remaining deferred items are OS-signed MSI/DMG/AppImage GUI installers and external multi-hundred-atom comparative SOTA benchmarks.
