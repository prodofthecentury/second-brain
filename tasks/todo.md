# Task Queue: Unified Local AI Brain

## System safety pass — 2026-10-24

Implemented and verified in this pass: loopback-only dashboard binding; a
model-output trust reset; source quote/hash anchors and exact-revision approval;
compare-and-swap atom edits/reviews under one OS writer lock; lexical index
refresh in the mutation workflow; stable-file ingestion checks; create-only
generated threads and captures; bounded, opt-in local MCP; cosine metric
manifest/scoring; and truthful doctor/README readiness language. See
`docs/system-sweep-2026-10-24.md` for exact evidence.

Still open: full failure-injection audit, authenticated dashboard/MCP identity,
merge/supersession workflow, answer-level trap evaluation, broader owner-approved
retrieval coverage, and an approved Git migration baseline. One atom is now
owner-approved; sodium-ion remains pending. See the 2026-10-27 system sweep for
current status.

## Follow-up — 2026-10-27

- Chroma's native import issue was traced to the missing Microsoft Visual C++ v14 x64 runtime. The official signed runtime is installed; Chroma 1.5.9 imports and a real staged generation now serves cosine queries. The current manifest covers all 13 atoms at 768 dimensions.
- Fixed Windows staging activation by closing the builder-owned Chroma client before the directory swap. `uv run scripts/chroma_rebuild_canary.py` verifies close/swap/reopen, cosine ranking, and rollback-generation retention.
- Scout now connects only to the public IP addresses captured during URL validation. HTTPX does not inherit environment proxy settings; redirects remain disabled. Regression tests cover a real local HTTP server behind a synthetic pinned public hostname and mixed public/private DNS answers.
- Reviewed `structured-logging.md` and `sodium-ion-battery-cost-record.md` against their stored sources. Neither was owner-approved: one is a local excerpt without author metadata, and the other lacks the original article and is explicitly speculative.
- Added a seven-query provisional evaluation set (five positive, two traps). Candidate Recall@5 is 1.00 for lexical and dense; MRR is 0.90/1.00, nDCG@5 0.939/1.00, and median latency about 6.4/57.9 ms. These labels are not owner-curated and are insufficient to claim dense retrieval is superior. Both trusted paths returned zero memories on all seven because no atom has owner approval.
- Classified `tmp/pdfs/` as reproducible page previews from the PDFs in the design-study source register and added it to `.gitignore`; files remain on disk.
- Git still has no clean baseline: nothing is staged, prior vault-root paths show as deleted, and the new `brain/` tree/project remain untracked. Do not stage or commit raw/private Markdown without the owner's choice.

Current verification: 14 system safety tests, smoke test, real Chroma canary,
compileall, doctor, and retrieval evaluator passed. See
`docs/system-sweep-2026-10-24.md`, `docs/owner-review-sample-2026-10-27.md`,
and `docs/retrieval-evaluation-results-2026-10-27.json` for evidence.

The first 2026-10-27 follow-up bullets above record the state before owner
approval. Since then, `structured-logging.md` and the seven-query evaluation
set were owner-approved, and the trap evaluation exposed four trusted retrieval
false positives. A test-first literal Claim overlap gate now reduces those
measured trap retrievals to zero; see `docs/system-sweep-2026-10-27.md` for the
remaining answer-safety and evaluation gaps.

## Task 1: Add review lifecycle fields to memory formatting

**Description:** Ensure every generated or normalized atom has a
`review_status` field, defaulting to `pending`, while preserving existing atom
files that already contain a valid status.

**Acceptance criteria:**
- [x] New fallback atoms include `review_status: pending`.
- [x] `normalize_memory_document` adds missing `review_status`.
- [x] Invalid review statuses are normalized safely.

**Verification:**
- [x] `uv run scripts/smoke_test.py`

**Dependencies:** None

**Files likely touched:**
- `scripts/memory_format.py`
- `scripts/ollama_refinery.py`
- `scripts/smoke_test.py`
- `brain/house-rules.md`

**Estimated scope:** Medium

## Task 2: Add source archive manifest records

**Description:** Record where each processed raw capture was archived so an atom
can be audited back to its original source.

**Acceptance criteria:**
- [x] Processing a file writes an archive manifest entry.
- [x] Manifest records source filename, archive path, timestamp, and generated atoms.
- [x] Existing archive behavior remains non-destructive.

**Verification:**
- [x] `uv run scripts/smoke_test.py`
- [x] Single-source canary via `uv run scripts/archive_manifest_canary.py`

**Dependencies:** Task 1

**Files likely touched:**
- `scripts/ollama_refinery.py`
- `scripts/config.py`
- `scripts/smoke_test.py`

**Estimated scope:** Medium

## Task 3: Add vector rebuild command from Markdown atoms

**Description:** Make the recall layer explicitly rebuildable from the Markdown
source of truth.

**Acceptance criteria:**
- [x] Command rebuilds `atoms` vector collection from `brain/2-atoms/`.
- [x] Command works with ChromaDB or JSON fallback.
- [x] Rebuild reports count of indexed, skipped, and failed files.

**Verification:**
- [x] `uv run scripts/smoke_test.py`
- [x] Manual rebuild on current atoms: `uv run scripts/rebuild_vectors.py --json`

**Dependencies:** Task 1

**Files likely touched:**
- `scripts/vector_store.py`
- `scripts/rebuild_vectors.py`
- `scripts/smoke_test.py`

**Estimated scope:** Medium

## Task 4: Create review queue view over pending atoms

**Description:** Add a simple local way to list pending memories before building
heavier dashboard controls.

**Acceptance criteria:**
- [x] Pending atoms can be listed with filename, memory type, claim, confidence,
  importance, and sources.
- [x] Accepted, rejected, and merged atoms can be filtered.
- [x] Output is readable in terminal and easy to reuse in dashboard.

**Verification:**
- [x] `uv run scripts/smoke_test.py`
- [x] Manual list command against current atoms: `uv run scripts/memory_review.py --status pending`

**Dependencies:** Task 1

**Files likely touched:**
- `scripts/memory_review.py`
- `scripts/smoke_test.py`

**Estimated scope:** Medium

## Task 5: Add review status transitions

**Description:** Provide safe commands to accept, reject, mark needs-edit, or
merge memories without deleting source files.

**Acceptance criteria:**
- [ ] Review commands update frontmatter only.
- [ ] Merge records `superseded_by` and `supersedes`.
- [ ] Rejected memories remain on disk.

**Verification:**
- [ ] `uv run scripts/smoke_test.py`
- [ ] Manual transition on a temporary canary atom

**Dependencies:** Task 4

**Files likely touched:**
- `scripts/memory_review.py`
- `scripts/memory_format.py`
- `scripts/smoke_test.py`

**Estimated scope:** Medium

## Task 6: Centralize search behavior

**Description:** Move CLI/dashboard/MCP search behavior toward one reusable
memory search module.

**Acceptance criteria:**
- [ ] Shared search function returns structured results.
- [ ] Results include filename, content, metadata, and linked neighbors.
- [ ] Callers can filter by review status.

**Verification:**
- [ ] `uv run scripts/smoke_test.py`
- [ ] `uv run chat_with_memory.py` still starts and searches when Ollama is available

**Dependencies:** Task 5

**Files likely touched:**
- `scripts/memory_search.py`
- `chat_with_memory.py`
- `scripts/mcp_server.py`
- `dashboard/app.py`

**Estimated scope:** Large; split before implementation if needed
