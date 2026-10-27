# External Architecture Review — Pass 3

Reconcile this final primary-architect position. Your job is to find any remaining fatal flaw, then state a concise final architecture contract. Do not repeat earlier commentary, write code, or edit files.

## Reconciliation

We accept your reduction to three stores, one OS-enforced mutator for canonical/derived state, a runtime probe, and evidence-verification. We will not add an authoritative SQLite queue or lock table. Capture is an append-only new-file operation using collision-resistant IDs; only canonical atom transitions, derived-index updates, and derived thread builds require the singleton. Human Markdown edits remain allowed, are detected as external changes, and rebuild only the derived state.

We correct the trust wording: byte-offset/quote validation proves *citation integrity*, not semantic entailment. A model can attach a real quote that does not support its claim. Therefore the system must never call an atom fully "verified" merely because a quote exists.

Proposed evidence/trust state:

- **Evidence origin** (sticky and deterministic): `user_local`, `mcp_local`, `web_remote`, or `imported_unknown`.
- **Citation integrity** (deterministic): `anchored`, `missing`, `hash_mismatch`, or `invalid_offset`.
- **Claim state** (only behavior gate): `candidate`, `source_anchored`, `human_reviewed`, `superseded`, `rejected`, or `quarantined`.
- **Epistemic label** is human-set only (`observation`, `preference`, `hypothesis`, `claim`, `procedure`); no local model invents certainty/confidence scores.

`source_anchored` means the source and exact quotation survive integrity checking; it does **not** assert entailment. In Standard mode, human-reviewed content may be stated directly; source-anchored material may be used only with explicit attribution and an answer-level `unreviewed evidence` marker. Research mode additionally shows candidate/quarantined material as leads and never turns it into factual prose. `web_remote` and `mcp_local` cannot leave `quarantined` without a human action. This avoids making a small local model pretend to score nuanced truth while preserving useful recall of fresh personal material.

The immutable file plane comprises evidence plus an append-only decision ledger. The Markdown atom holds the current human-readable projection, but its mutable behavioral state is derived from the ledger. A transition writes a durable ledger event first, then atom snapshot via atomic replacement; if interrupted, reconciliation replays the ledger. SQLite contains only disposable FTS/vector/telemetry projections. This is intentionally an event/snapshot design; its cost must justify the audit/recovery value.

The current hardware profile has an **absent** generation path until the runtime probe sees both a compatible local server and an explicitly installed model. It still delivers capture, manifest/audit, FTS search, review, and citation integrity. Dense retrieval is a separately gated index generation; no hash fallback. 3B CPU drafting is an explicitly initiated, batch job only, with strict byte/token ceilings and cancellation. Vision, cross-encoder reranking, semantic graph expansion, automatic threads, and 7B+ generation are unavailable—not UI toggles—on this profile.

## Final challenge

1. Is the ledger/snapshot architecture justified in the first implementation slice, or should it be a later upgrade after simple frontmatter atomic writes plus backup prove insufficient? Give a hard yes/no and explain the failure mode that decides it.
2. Identify whether the answer-policy compromise is safe enough. In particular, attack the boundary between an attributed source-anchored statement and a factual assertion.
3. Define the minimum *non-negotiable* first release: no more than seven slices, each independently testable, that turns the current code into a trusted offline second brain on the actual laptop.
4. Give a final red-team system sweep specification: static checks, failure injections, data-integrity audit, security tests, retrieval evaluation, performance test, and runtime readiness. For each: exact pass condition and what blocks release.
5. List decisions that must stay with the human owner rather than an agent/model.

Return only the challenge findings and a precise final recommendation. Do not use false certainty.
