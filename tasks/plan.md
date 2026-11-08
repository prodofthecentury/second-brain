# Implementation Plan: Dependable Local Brain Pilot

**Plan status:** Active. Reconciled with source and the 2026-10-27 system sweep.
This plan describes a trustworthy local pilot first. The one-click installer,
online hosts, and owner-built host adapters remain a later product horizon.

## Verified starting point

- 14 canonical Markdown atoms; two current owner-approved atoms
  (`structured-logging.md`, `design-for-failure.md`). Sodium-ion remains
  pending because its primary source is missing and the capture is tentative.
- Native Chroma works on this Windows host. The active cosine generation is
  `ce48706db17d440fbba04b8c7f86c37e` (`nomic-embed-text`, 768 dimensions),
  with 14/14 atoms indexed. The dense rebuild canary and live query evaluation
  pass.
- The owner-approved query labels cover five candidate-ranking positives and
  two traps. Several positive targets remain pending atoms, so they are valid
  only as exploratory raw-candidate diagnostics, not as approved-answer
  examples. `structured-logging.md` and `design-for-failure.md` currently
  supply trusted positive cases. Lexical/dense raw candidate metrics are
  1.00/1.00 Recall@5, 1.00/1.00 MRR, and 0.975/1.00 nDCG@5. Repeated answer
  scoring now covers two positives and two traps, but the set remains too
  small, has no hybrid mode or holdout cases, and only one model/host has three
  repetitions. Scores remain provisional.
- CLI and dashboard structurally abstain without approved recall context and
  validate exact model-selected claims against current approved atoms and
  source anchors before display. Repeated local-model answer evaluation now
  covers two positive and two trap cases; this is still a tiny pilot set.
- Review CAS transitions, exact source anchors, a shared writer lock, FTS
  refresh, and an append-only decision log exist. Two-atom merge/supersession
  behavior and lifecycle failure cases are incomplete.
- `scripts/recall.py` is the shared structured search contract used by CLI,
  dashboard, and MCP. `RecallOptions` supports review status and context
  filters, but caller-facing scope/filter controls and explanations are
  incomplete. MCP and dashboard have unresolved identity/authentication gaps.
- There is no `pyproject.toml`/`uv.lock`; launchers resolve dependencies at
  runtime. Commit `db9d182` is a code-only checkpoint; the roadmap/review
  preparation in this task is uncommitted. Commit `bffca9e` still contains 18
  old-layout vault files. No remote is configured; do not publish until that
  history is reviewed or scrubbed.
- `tmp/` contains four reproducible PDF page-preview PNGs. They are retained
  locally and ignored by Git.

Evidence: `docs/system-sweep-2026-10-27.md`,
`docs/owner-review-sample-2026-10-27.md`,
`docs/retrieval-evaluation-results-2026-10-27.json`, current source, and the
verification commands recorded in the sweep. The sweep is authoritative when
older task notes disagree.

## Architecture and trust rules

- Markdown atoms and immutable evidence files remain durable source of truth;
  lexical/vector indexes are disposable projections.
- Model output never approves, merges, deletes, or promotes memory. Owner
  decisions remain explicit, hash-bound, and auditable.
- `standard` answers may state only current owner-reviewed evidence. `research`
  mode may expose unreviewed material only as clearly labelled leads, never as
  factual claims.
- Citation integrity proves that a quote is anchored to bytes; it does not
  prove that the source entails the claim.
- Any answer claim and citation must be validated outside the model before
  display. If validation fails, suppress the invalid claim or abstain.
- Keep lexical retrieval available without an embedding service. Dense
  retrieval remains optional until representative owner-labelled results and
  hardware latency justify it.
- Dashboard access stays loopback-only. Do not expose the unauthenticated
  dashboard or current MCP server to a cloud-connected client.
- Do not add owner data to Git or rewrite the old history without a verified
  recovery copy. Model downloads, owner review decisions, and external sharing
  require explicit authorization.

## Dependency-ordered tasks

### Goal 0 — Reconcile roadmap with implementation — COMPLETE

**Outcome:** The queue reflects live behavior and points to the next bounded
change.

**Existing behavior:** The old queue calls review transitions and shared recall
unimplemented. The source already has CAS approve/reject/quarantine/demote and
edit operations, source anchors, and shared `search_memory`; it has no merge
operation, and consumers do not expose all supported filters.

**Changes:** Replaced stale checklist items and the 2026-10-24 plan with this
verified baseline and the ordered tasks below. Historical completion evidence
is retained in the system-sweep and commits.

**Acceptance criteria:** Plan and task queue use the same IDs, status,
dependencies, and acceptance scope; deferred product work is separated.

**Dependencies:** None.

**Verification:** Compare every formerly unchecked `tasks/todo.md` item against
current source and sweep; ensure no stale unchecked Task 5/6 remains.

**Likely files:** `tasks/plan.md`, `tasks/todo.md`.

### Goal 1 — Curate reviewed examples and a versioned evaluation set — IN PROGRESS

**Outcome:** The owner can review a compact, source-backed sample across
multiple memory types, and every positive benchmark label refers to a current
owner-approved atom revision.

**Existing behavior:** Two atoms are approved. Dataset `second-brain-retrieval-pilot`
version `2.0.0` now has two answerable cases pinned to exact approved atom
revisions, three explicitly exploratory candidate-ranking cases, and two
traps with abstention reasons. All seven cases are regression cases; there are
no development or holdout examples yet. The validator rejects stale revisions,
unapproved answer targets, ambiguous labels, and incomplete traps. This is a
useful contract, but seven cases cannot support broad quality claims. Six atoms
were reported as locally anchorable, but source review or an owner's truth
decision is not implied by an anchor.

**Changes:** Prepare a small batch spanning a factual claim, procedure,
preference, and project decision where durable owner-provided evidence exists.
Verify each source and claim, present the owner with explicit
approve/revise/reject decisions and rationale, and keep missing or ambiguous
evidence pending. Do not invent preference/decision memories to fill category
quotas; if the needed source is absent, record the gap and request owner input.
Resolve how owner-reported preferences may be returned without presenting them
as objective facts, because the current mutator deliberately refuses to approve
`epistemic_label: preference`. Extend the dataset schema with case category,
answerability, expected atom IDs, acceptable alternatives, expected claims,
abstention mode, trap rationale, dataset version, split, and atom revision
hashes. Preserve candidate-ranking relevance separately from answerability;
pending atom labels can only be exploratory. Do not infer a label from model
output.

**Acceptance criteria:** Every scored positive answerability case references a
currently approved atom revision and source-backed expected claims; pending
target atoms remain in a separately named exploratory retrieval set; every
trap has a documented no-answer reason; preference and project-decision cases
are included only after explicit owner review and a defined non-factual answer
mode where needed; unresolved examples stay pending and out of the positive
answerability set. The evaluator binds results to the dataset hash/version and
index generation, reports candidate relevance separately from expected
answerable atom coverage, and fails closed when the dense embedder is
unavailable. The current seven regression examples do not complete this goal:
development and holdout examples are still required.

**Dependencies:** Goal 0.

**Verification:** Source/anchor audit; owner decisions recorded in the
decision log; dataset validator rejects pending, stale, or unlabeled cases;
manual label audit before evaluation.

**Likely files:** `docs/owner-review-sample-*.md`,
`docs/retrieval-evaluation-proposed.json`, `scripts/evaluate_retrieval.py`,
`scripts/memory_review.py`, atom/source files only with owner approval.

### Goal 2 — Enforce and measure answer-level grounding — IN PROGRESS

**Outcome:** Standard answers display only claims supported by current approved
evidence, with citations rendered from verified program data; unsupported or
invalid claims are withheld.

**Existing behavior:** Empty-context CLI/dashboard requests abstain before
calling the model. The CLI and dashboard request JSON claim drafts through
Ollama's JSON-schema mode and withhold raw model output until validation
completes. The model returns only exact claim strings; the validator maps each
to retrieved atom text, then checks current atom bytes, canonical owner
approval, and source anchors. Program data supplies source excerpts, hashes,
and offsets. This intentionally rejects model paraphrases. A repeated answer
evaluator now measures expected claim coverage, unexpected displayed claims,
trap leakage, abstention, citation coverage, and end-to-end latency. There is
no research/lead answer mode, and the pilot set is too small for broad claims.

**Changes:** Extend typed request/claim/evidence/result contracts; assemble a
bounded evidence packet; request schema-constrained claim drafts; validate that
each exact claim maps to retrieved, current, eligible atom bytes and a valid
source anchor; render provenance outside model prose. Keep standard and
research modes separate. `scripts/evaluate_answers.py` runs repeated model
outputs against answerable and trap cases and records supported/unexpected
claims, citation coverage, abstentions, and latency without saving raw drafts.

**Acceptance criteria:** No eligible evidence means no model call; every
displayed standard factual claim maps to current approved evidence; fabricated
or stale citations are rejected; the UI exposes atom and source details; traps
are evaluated across repeated complete outputs; invalid drafts never display as
valid answers.

**Dependencies:** Goal 1 dataset format and reviewed examples.

**Verification:** Test empty evidence, approved evidence, foreign/stale/fake
citation, stale/revoked source, unsupported paraphrase/additional claim,
malformed structured output, repeated trap outputs, and UI provenance
rendering; run answer evaluator on the frozen regression split. Current tests
cover core validation failures; the live Ollama-backed path has been exercised
for three repeats on two answerable and two trap cases using `qwen2.5-coder:7b`.
Dashboard startup,
loopback binding, health, and page response have been checked, but its chat
interaction has not been exercised with a live model.

**Likely files:** `scripts/answer_policy.py`, a dedicated answer evaluator,
`scripts/recall.py`, `chat_with_memory.py`, `dashboard/app.py`,
`scripts/smoke_test.py`.

### Goal 3 — Complete routine review and trust maintenance — IN PROGRESS

**Outcome:** Owners can safely inspect, edit, approve, reject, quarantine,
demote, and explicitly merge atoms while keeping sources and history auditable.

**Existing behavior:** Review queue, source anchors, CAS edits/transitions,
atomic replacement, writer lock, revision backups, decision log, and index
refresh exist. Approval is tied to an exact current atom/source revision.
`scripts/audit_sources.py` now inventories stored/current anchor hashes and
source status; its default is read-only. With explicit `--demote-stale --actor`,
it demotes accepted atom revisions whose current approval no longer verifies,
using the canonical mutator and audit log. `apply_atom_merge` now performs an
explicit owner-directed two-atom supersession with exact CAS on both revisions,
bidirectional links, backups, pending re-review of the survivor, and paired
append-only audit records. The Vault Explorer review panel now displays the
atom revision hash, stored evidence excerpt, pinned source path/hash/byte range,
current source quote/hash, anchor validity, and linked atom status. Stale
accepted approvals can be explicitly demoted by CAS from the UI. Merged atoms
are blocked from later approval through the generic transition API. The
dashboard still lacks an owner-facing merge control and broader
quarantine/reject/demote workflow.

**Changes:** The source-integrity CLI and explicit stale-approval demotion are
implemented. Add an owner-facing merge control and complete the
quarantine/reject/demote workflow; explain source-change consequences where not
yet shown.

**Acceptance criteria:** Source edits/removal invalidate affected trust; stale
approvals leave recall; rejected/superseded atoms remain auditable but not
retrievable; concurrent edits fail without clobber; merge requires an explicit
owner-selected survivor and preserves both files; decision log captures both
revisions and rationale.

**Dependencies:** Goal 1 reviewed sample. Goal 2 answer contract must continue
to enforce current revisions.

**Verification:** Failure injection covers second-atom write, lexical refresh,
and decision-log append rollback; tests cover CAS conflict on the second input,
paired audit, merged-atom reapproval rejection, source hash/offset changes, and
path traversal rejection in review details. Still verify end-to-end recall
exclusion and merge crash recovery; temporary canaries only.

**Likely files:** `scripts/atom_mutator.py`, `scripts/review_atom.py`,
`scripts/evidence.py`, `scripts/memory_review.py`, `dashboard/app.py`, tests.

### Goal 4 — Close dashboard and MCP access gaps

**Outcome:** Local interfaces are safe under a documented local-user threat
model and cannot be triggered cross-origin or exposed accidentally.

**Existing behavior:** Dashboard now binds loopback and uses a local owner
password gate plus explicit origin/session protections. MCP is disabled by
default and narrow, but has no client identity/authentication. Cloud MCP use is
unsupported.

**Changes:** Document threats and supported access model before choosing
controls. The threat model now identifies vault/evidence disclosure, forged
owner actions, cross-site WebSocket/session abuse, local same-user processes,
and accidental network exposure. A local password gate now blocks dashboard
content until unlock; its salted scrypt verifier lives in per-OS-user app state,
sessions expire after 30 idle minutes, and an owner can lock the page manually.
Loopback bind, CORS, XSRF, strict XSRF cookie, and allowed host/origin settings
are explicit. This is not protection from malware or another process running
as the same OS user. Proxy/tunnel use remains unsupported. Define trusted local
MCP launch configurations and audit tool use without recording private query
contents.

**Acceptance criteria:** Unauthenticated dashboard sessions cannot read
vault data or trigger actions; cross-origin state changes are rejected;
MCP cannot enumerate the vault or promote content; MCP is opt-in and restricted
to approved search plus bounded untrusted capture; external content remains
quarantined.

**Dependencies:** Goal 3 review and mutation boundaries.

**Verification:** Password verifier/login and live locked/unlocked/manual-lock
checks pass; effective config was inspected. Browser-origin request
verification remains open, as do MCP capability/scope tests and external
quarantine regression.

**Likely files:** `dashboard/app.py`, `run_dashboard.ps1`,
`scripts/mcp_server.py`, MCP policy docs, security tests.

### Goal 5 — Make setup, health, and recovery reproducible

**Outcome:** A fresh supported environment can install the core predictably,
diagnose optional capabilities, and recover indexes without losing durable
memory.

**Existing behavior:** Runtime health and atomic vector generations are
reported; lexical search is model-free. The project now has a Python 3.11
package, committed `uv.lock`, split optional extras, a `second-brain` bootstrap
CLI, wheel packaging for scripts and dashboard, and a Windows setup launcher.
An isolated Windows wheel install initialized a user-selected vault, but a
clean-machine setup-script run, non-Windows verification, recovery matrix, and
complete capability report still remain.

**Changes:** Maintain the manifest and lock; verify the Windows one-command
bootstrap on a clean profile; report Python/OS/CPU, optional packages, Ollama
models, lexical and vector model/metric/dimension/generation, atom/approval
counts, and degradation reasons; document and test a recovery matrix for stale
index, unavailable model, partial rebuild, and missing native Chroma runtime.

**Acceptance criteria:** Fresh checkout and wheel setup are reproducible on all
declared platforms; install starts a local interface with no model required;
same inputs produce equal indexed content/metadata; failed rebuild preserves
the old usable generation; health precisely identifies usable paths; optional
model absence does not report false readiness or substitute embeddings.

**Dependencies:** Goal 2 contracts and Goal 3 mutation/index consistency.

**Verification:** Isolated wheel install, fresh-profile bootstrap on each
supported OS, offline lexical-only path, stable rebuild comparison,
interrupted/failed rebuild rollback, doctor capability matrix, runtime
launcher health check.

**Likely files:** `pyproject.toml`, `uv.lock`, `scripts/cli.py`,
`scripts/doctor.py`, `scripts/index_manifest.py`, rebuild scripts,
`setup.ps1`, and setup/recovery docs.

### Goal 6 — Expose project-scoped recall consistently

**Outcome:** CLI, dashboard, and MCP can apply the same project/context and
review filters and explain why each result matched.

**Existing behavior:** Shared `RecallOptions` and structured `RecallResult`
already support contexts/status filtering and linked neighbors. Callers mostly
hard-code defaults; context IDs are not governed; match reasons report status,
type, and optional context but not consistently lexical/dense/link provenance.

**Changes:** Choose stable `project:<id>` context IDs; expose scope and status
filters across callers; add retrieval-source and linked-neighbor explanations;
add duplicate suggestions as a read-only review aid with content hashes,
claims, provenance, status, and revisions. Keep all duplicate actions owner
controlled.

**Acceptance criteria:** Project-scoped search returns only eligible project
memories; CLI/dashboard/MCP return one structured contract; each result has
concrete match explanation; duplicate suggestions never mutate files or trust;
offline project retrieval works.

**Dependencies:** Goal 5 stable contracts and setup; Goal 2 answer request
scope.

**Verification:** Same fixtures through each interface, wrong-project and
status-filter cases, dense/lexical/link explanation cases, duplicate false
positive review, no-network offline check.

**Likely files:** `scripts/recall.py`, `chat_with_memory.py`,
`dashboard/app.py`, `scripts/mcp_server.py`, duplicate suggestion module/tests.

### Goal 7 — Expand and decide retrieval modes by evidence

**Outcome:** Dense remains optional or becomes default only when held-out owner
queries show material benefit within a measured machine-specific latency and
resource budget.

**Existing behavior:** Five positive and two trap queries compare lexical and
dense raw candidate rankings. No separately labelled hybrid mode, held-out
split, p95, answer grounding, or representative scale corpus exists.

**Changes:** Grow the source-reviewed corpus in deliberate owner-approved
batches; add versioned regression/development/holdout datasets; compare
keyword, dense, and combined rankers; report Recall/MRR/nDCG, traps, attribution,
end-to-end answer quality, p50/p95 latency, and resource use. Keep weights
provisional and tune only on development data.

**Acceptance criteria:** Results are reproducible and bound to dataset version,
atom revisions, model, and generation; pending/rejected memories earn no
relevance credit; every mode is reported separately; dense-default decision
uses representative holdout performance and a declared latency target.

**Dependencies:** Goals 1, 2, 5, and 6.

**Verification:** Dataset integrity validator, deterministic evaluator,
held-out manual error review, trap answer evaluation, p50/p95 and resource
measurements on this host.

**Likely files:** `docs/retrieval-evaluation-*`, `scripts/evaluate_retrieval.py`,
answer evaluator, `scripts/recall.py`, performance reports.

### Goal 8 — Independent review and local pilot release gate

**Outcome:** An independent read-only critic and a primary-agent evidence audit
agree that the local pilot meets explicit release criteria or identify remaining
blockers.

**Existing behavior:** Prior Claude review exists, but the last requested
read-only review attempt failed because the saved CLI OAuth session expired.
The current prototype is not release-ready.

**Changes:** Restore reviewer authentication when available; provide actual
current changed contracts/source/evaluation evidence; request adversarial
review; verify every finding; fix or record justified trade-offs; run final
self-audit.

**Acceptance criteria:** Foreign/fake vectors absent; evidence/source/citation
audits have no unexplained failures; repeated answer traps produce no
unsupported factual claims; external content cannot self-promote; unavailable
capabilities fail visibly; shipped dashboard setup and recovery work on this
host; all findings are reconciled.

**Dependencies:** Goals 1–7.

**Verification:** Full static, behavioral, failure-injection, integrity,
security, retrieval, performance, and runtime sweeps; independent read-only
Claude review; primary-agent review of Claude's conclusions.

**Likely files:** current code/tests/docs plus final sweep and reviewer record.

## Deferred product horizon

Goal 5 now establishes reproducible package and local-UI setup. After Goal 8,
extend that foundation into a stable host-independent memory API, narrow local
and online AI adapters, owner-built host support, production cross-platform
installers, and hardware capability profiles. Local/offline remains
first-class; online data sharing is explicit per host. “State of the art” must
be demonstrated against current alternatives on representative tasks and
declared hardware, not asserted. The current package is not yet an arbitrary-AI
plugin, and Windows setup does not install an LLM runtime or model.

## Checkpoints

- **After Goals 1–2:** reviewed labels are valid and answer-level grounding
  rejects unsupported output before display.
- **After Goals 3–5:** mutation, interface access, and rebuild/recovery paths
  pass their adversarial and failure-injection cases.
- **After Goals 6–7:** project workflows are consistent and retrieval choices
  follow held-out quality and latency evidence.
- **After Goal 8:** decide release readiness from independent evidence; do not
  equate passing smoke tests with release approval.

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Labels expand faster than owner review | High | Positive labels require current approved atom revisions |
| Citation is mistaken for entailment | High | Validate structured claims and separately score support |
| Interface hardening locks out local users | Medium | Threat-model framework controls and retain local-only mode |
| Dependency lock omits native runtime needs | High | Verify fresh setup and document OS runtime prerequisites |
| More advanced retrieval increases latency without value | Medium | Holdout benchmark and machine-specific SLO |
| Installer/adapter work distracts from trust core | High | Keep product horizon deferred until Goal 8 |
