# Codex CLI Goal — Copy Everything Below

You are continuing a serious local-first AI second-brain project. Work in this repository:

`C:\Users\camer\.gemini\antigravity\antigravity_projects\projects\second_brain`

Before taking implementation actions, read **all** of `docs/codex-cli-handoff.md` and `docs/learning-and-memory-design-synthesis.md`, then inspect the current worktree and runtime yourself. The handoff is essential context, but current files and command output are authoritative.

## Objective

Build the most capable **trustworthy, private, local-first, offline-capable** AI second brain this actual machine can support. It must be smart, useful, efficient, fast where the hardware permits, and transparent about what it does not know. Do not ship generic RAG, a cosmetic dashboard, or an AI that silently makes degraded output look correct.

### Long-term product target clarified by the owner on 2026-10-27

The eventual deliverable is a **one-click installable Brain plugin for any PC**
that supports local/offline and online AI hosts, including owner-built systems.
Offline mode must be a first-class, state-of-the-art target, not a weak fallback.
Online mode may offer stronger models and additional capabilities, with data
sharing explicit and scoped by host; private vault data is never silently
uploaded. Installation must probe each PC and report what it can run instead of
promising identical capabilities on all hardware. Treat this as a host-adapter
product around a stable, testable memory core, not as a claim that one protocol
already works with every AI. “Better than anything available today” is an
evaluation target, not a marketing claim: define representative tasks and
compare against current alternatives before making it. The current repository
is a local prototype and does not yet meet this product target.

The durable Markdown vault remains the human-readable long-term memory. Raw evidence must be preserved. Derived indexes must be safely rebuildable. A local AI model may assist, but it must never silently create trust, decide truth, delete memories, promote external content, or rewrite user knowledge without owner approval.

The base model is fixed unless the owner explicitly approves a separate training experiment. Do not confuse parameter training with in-context adaptation, reviewed durable memory, or bounded workflow improvement. The source study and its implications are recorded in `docs/learning-and-memory-design-synthesis.md`.

## Goal milestones and completion criteria

### 1. Establish a trustworthy baseline

- Record the current repository state, runtime versions, configuration, and index manifest.
- Document how to rebuild the index from the vault and how to verify the result.
- Confirm a clean rebuild produces the same indexed atoms and metadata from the same inputs.
- Write down what is deliberately excluded, including any reviewed `tmp/` contents.

**Done when:** someone can start from a fresh checkout, follow the documented steps, and reproduce a verified index without relying on undocumented local state.

### 2. Complete a small, source-backed approval batch

- Select a manageable sample of atoms, including different kinds of content and at least a few ambiguous cases.
- For each one, check its claims and provenance against the source.
- Record the owner’s decision and rationale: approve, reject, or revise.
- Fix extraction or provenance problems before approving affected atoms.
- Keep unreviewed atoms unavailable to retrieval.

**Done when:** every decision in the sample has source evidence and an explicit owner decision, with no approval inferred from silence or automation.

### 3. Build and run a retrieval evaluation

- Create owner-curated queries with expected relevant atoms, acceptable alternatives, and trap queries that should return nothing useful.
- Include cases for dense-only retrieval, keyword-only retrieval, and the combined ranker.
- Measure recall and ranking quality at useful cutoffs; inspect false positives and missed matches manually.
- Keep the current score weights labeled provisional until results support changing them.
- Save the evaluation set and results so future changes can be compared.

**Done when:** there is a repeatable evaluation run, known failure cases, and a documented baseline. Don’t tune weights just to improve a tiny or unrepresentative set.

### 4. Validate the full retrieval path

- Test the real query path against the reviewed, approved sample.
- Verify that stale or unapproved content cannot leak into results.
- Check behavior for empty results, duplicates, malformed metadata, and index/vault disagreement.
- Confirm rebuild and query work in the intended runtime, not only in an isolated canary.

**Done when:** an owner can ask representative questions and inspect why each returned atom matched, while rejected and unreviewed atoms remain excluded.

### 5. Define safe operating and recovery procedures

- Specify how to detect an invalid or stale index and how to recover from it.
- Verify rollback behavior after an interrupted or failed rebuild.
- Define what happens when embeddings, Chroma, or the source vault are unavailable.
- Add clear diagnostics for index generation, source counts, failures, and query behavior.

**Done when:** rebuild failures are visible, the previous usable index can be recovered, and the operator has a documented procedure rather than guesswork.

### 6. Set maintenance rules before expanding the vault

- Decide how new and changed atoms enter review.
- Define when approval expires or must be revisited after source changes.
- Decide how deletions propagate into the index.
- Keep a simple audit trail for approvals, rejections, and rebuilds.

**Done when:** vault changes cannot silently turn into trusted searchable content, and the index can be reconciled with its sources.

## Next goal set: Scale the trusted pilot into a useful daily system

### 1. Close the remaining security gaps

- Finish Scout DNS-rebinding protection and dashboard/MCP identity before increasing automated or external ingestion.

### 2. Make trust maintenance routine

- Build a repeatable review flow for new atoms.
- Re-gate approvals when their sources change.
- Define deletion and merge behavior.
- Keep an audit trail for decisions and rebuilds.

### 3. Make recall useful across daily workflows

- Keep CLI, dashboard, and MCP behavior consistent.
- Add project-scoped recall and clear explanations of why each atom matched.
- Suggest duplicates without merging them automatically.

### 4. Expand based on measured results

- Grow the reviewed corpus and evaluation set in deliberate batches.
- Keep dense retrieval provisional unless it beats the keyword baseline on representative queries and acceptable latency.

## Mandatory collaboration with Claude

Use the installed Claude Code CLI and the strongest available Claude model (`--model opus --effort max`) as a real, read-only architectural critic throughout the build. Do not do one token exchange and call it collaboration.

- The prior Claude session ID is `4af778e0-84c5-4e82-bda6-510a173a69de`; resume it if available so it retains the prior reviews.
- Before each Claude review, give it the actual artifact/contract and ask it adversarially to find failures, not validate the work.
- After every material Claude finding, reconcile it against source and runtime evidence. Fix valid issues or document an explicit trade-off.
- Use Claude for: architecture decisions, each high-risk slice, pre-release review, system sweeps, and a final independent sweep.
- Restrict Claude to read-only tools unless the owner explicitly authorizes writes. A safe invocation shape is documented in `docs/codex-cli-handoff.md`.

Do not artificially wait or pad elapsed time. This is expected to take real sustained work; prove depth through small implemented and verified slices, evidence, reviews, and tests.

## Hard architectural contract

1. **Fail closed.** Capture/archive can work while models are unavailable. Extraction, embedding, critic, synthesis, and normal semantic recall must visibly defer if their real model is unavailable. Never write a fake/hash fallback embedding into a served index.
2. **Three-store boundary.**
   - Immutable evidence files plus append-only decision records are durable.
   - Markdown atoms are durable human-readable memory.
   - SQLite FTS/vector/telemetry is only a rebuildable projection and may never be the sole record of evidence or a user decision.
3. **Index integrity.** Every vector must carry model identity, dimension, content hash, and index generation. Never compare dimensions or generations. Build a new generation, evaluate it, then atomically select it.
4. **Trust is structural.** Model confidence is not trust. Add deterministic origin and citation-integrity facts plus owner-controlled claim/review state. Source-anchored does not mean true. A model must not be trusted merely to emit a disclaimer.
5. **Human edits re-gate trust.** Every edit to an existing atom is a canonical compare-and-swap mutation. It rechecks citation integrity and demotes previously trusted state until re-anchored/re-affirmed.
6. **External isolation.** Web Scout and MCP data must be quarantined, retain sticky provenance, have strict size/network controls, and require an owner action to promote. MCP is local, consented, scoped, and read-only by default; do not enumerate the vault to cloud clients.
7. **Single writer.** Existing atom transitions and derived-index updates are coordinated through one OS-enforced mutator. Capture creates new immutable files only. No blind overwrites and no concurrent JSON/atom mutations.
8. **Hardware truth.** Capability is runtime-probed. On this laptop, lexical search/capture/review are baseline. Vision, heavy reranking, semantic multi-hop expansion, automatic threads, real-time extraction, and 7B+ default generation are absent until a measured profile proves them viable.
9. **No autonomous irreversible authority.** The owner alone decides entailment review, epistemic labels, promotion, rejection, merge/supersession, deletion, schema-breaking migrations, model/index upgrades, and non-local MCP/network policy.

## Required implementation order

Work in small, test-first, independently verified slices. Do not run queued ingestion, Scout, daemon, night shift, MCP, or a destructive vector rebuild until their relevant safety gate is implemented and tested.

1. Truthful runtime baseline: repair dashboard launcher; capability/index health; remove silent hash embedding fallback.
2. Immutable evidence manifests: hashes, origin, custody, resolvable sources; non-destructive.
3. Model-free lexical FTS retrieval: shared recall contract and frozen owner-labelled evaluation set.
4. Citation integrity and claim state: offsets/hashes, edit-triggered re-gating.
5. Safe review transitions: atomic replace, OS singleton, compare-and-swap, flat decision record.
6. Structural answer assembly: Standard vs Research views, non-model provenance chrome, trap-query tests.
7. Safe automation: Scout/MCP quarantine, SSRF defense, scoped MCP, explicit bounded/cancellable drafting.
8. Dense retrieval only if it beats lexical baseline within a measured latency SLO on this laptop.

## Mandatory verification and stop condition

For every slice, write a failing behavioral test first, implement minimally, run focused tests, then the full smoke/build verification. Record exact commands and results in the workspace.

Before any “trusted/releasable” claim:

- Run local static, behavioral, failure-injection, data-integrity, security, retrieval-evaluation, performance, and runtime-readiness sweeps.
- Have Claude Opus run a separate read-only system sweep of the actual source and test evidence.
- Reconcile every Claude finding with evidence, fix valid findings, then perform one final independent self-sweep of Claude’s conclusions.
- Do not claim clean merely because smoke tests pass.

The release gate is not met until: foreign/fake vectors = 0; evidence/source/citation audits = 0 failures; trap-query fabrication rate = 0; external content cannot auto-promote; unavailable capabilities cannot run; and the dashboard launcher works as shipped on this laptop.

## Working rules

- Preserve user data. Never delete or move raw captures/atoms without a verified reversible migration and owner approval.
- Keep Markdown as source-of-truth. Do not place authoritative memory or user decisions only in a database.
- Do not install large models, change retention, initialize/commit Git, or connect a cloud MCP host unless the owner explicitly authorizes it.
- Do not hide current limitations. Test on the actual machine and report exact evidence.
- Keep `docs/codex-cli-handoff.md`, the architecture draft, plan, task list, and sweep results up to date as decisions change.
