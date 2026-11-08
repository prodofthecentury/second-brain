# Owner review batch proposal — 2026-10-27

**State:** Updated after explicit owner actions on 2026-10-27. The
failure-handling claim is approved; the procedure correction is applied but
pending; the product target is captured as a pending project-decision draft.
No other atom was approved by inference.

## Why this batch is needed

The vault has 14 atoms and two current owner-approved revisions. The current
evaluation set has five positive candidate-ranking labels, but four of their
target atoms are pending; those labels are exploratory retrieval diagnostics,
not approved-answer examples. The current canonical schema supports memory
types `semantic`, `episodic`, `procedural`, and `self`, plus epistemic labels
including `preference` and `procedure`. It has no explicit project-decision
type. The review mutator also refuses to approve `epistemic_label: preference`
for factual standard recall.

## Source-backed candidates

### Existing approved factual example: `structured-logging.md`

This revision is already approved by the owner and anchored to the exact quote
in `0-raw/archive/2026-07/high-level-engineering-practices.md` (bytes
1398–1525). No new decision is requested. Together with the approved
`design-for-failure.md` claim, it is one of the two current trusted
answerability examples.

### Factual claim: `design-for-failure.md`

- Current state: accepted; `find_evidence_anchor` resolves one unique exact
  excerpt in `0-raw/archive/2026-07/high-level-engineering-practices.md`, bytes
  1044–1203.
- Claim: “High-level systems should be designed to handle failures
  gracefully.”
- Supporting excerpt: “Assume the network will fail, the database will go
  down, and the API you depend on will timeout. Implement retries, exponential
  backoffs, and circuit breakers.”
- Review limitation: this is a local capture with no author/publication
  metadata. The anchor proves the excerpt is present, not that the guidance is
  authoritative or universally correct.
- Owner decision: approved the exact current claim. The canonical mutator wrote
  actor `camer`, the exact atom revision hash, source hash/byte anchor, backup
  path, and timestamp `2026-10-27T18:17:10` to
  `brain/0-raw/archive/decision-record.jsonl`. `approved_revision_is_current`
  returns true for this revision.

### Procedure candidate: `controlled-refinery-change-validation.md`

- Current state: pending and tentative. Its source
  `0-raw/archive/2026-07/cognitive-core-validation.md` exists. The old Evidence
  field included quotation marks and an attribution suffix, which prevented
  anchoring. The atom now contains the corrected exact Evidence text: “To validate a Refinery change safely, process
  exactly one controlled capture, inspect the generated memory atoms, and only
  then process the full inbox.”
- Read-only validation: that proposed excerpt resolves uniquely to the source
  at bytes 248–399 (SHA-256
  `3c804d81b48e9d91a02776e32f50afb4ee9fd083ff75781e99c9bc20acd9cf9f`).
- Review limitation: this is an internal project capture. It supports the
  procedure as recorded, but does not prove it is still the preferred current
  workflow. A revision should also label `epistemic_label: procedure` rather
  than inherit the current default `claim`.
- Owner decision: authorized applying the exact evidence correction and
  `epistemic_label: procedure`; keep it pending for a separate approval
  decision. The canonical CAS edit was recorded at `2026-10-27T18:17:29` and a
  follow-up metadata correction at `2026-10-27T18:18:33`. The source excerpt
  now anchors uniquely, but the atom remains `citation_integrity: missing` and
  `review_status: pending` until owner review. Its origin is
  `imported_unknown`; no approval has been inferred.

## Missing owner-originated categories

### Preference

No separate preference atom exists. The captured product-decision excerpt
also reflects a preference for first-class offline capability, but the current
mutator blocks `epistemic_label: preference` approval for factual standard
recall. Keep this preference out of standard positive-answer evaluation until
an attributed-preference answer mode is defined and the owner reviews that
classification. The project-decision draft does not silently double as an
approved preference.

### Project decision

The owner authorized capturing the explicit product target in
`CODEX_CLI_GOAL.md`. A local excerpt was added at
`brain/0-raw/owner-product-target-2026-10-27.md`; it records the source path and
original document SHA-256. A pending episodic draft was created at
`brain/2-atoms/owner-product-target.md` with `contexts: [project:second-brain]`
and verified source offsets/hash. It is classified as a project decision
because the excerpt states the intended product target. It remains
`claim_state: candidate`, `review_status: pending`; no approval was inferred.

## Evaluation handling until review completes

- Keep the seven-query version 2.0.0 dataset as the owner-approved query and
  candidate-ranking record. It separately marks two answerable cases, three
  exploratory cases, and two no-answer traps. The expected claims and exact
  approved atom revisions for answerable cases are pinned in the data.
- Do not score exploratory pending target atoms as positive standard-answer
  coverage; the evaluator enforces this distinction before scoring.
- Keep both traps as no-answer regressions.
- Add any fact, procedure, preference, or project-decision case to the
  approved-answer regression set only after its target revision and answer mode
  have been explicitly reviewed.
