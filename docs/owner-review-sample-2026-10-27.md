# Evidence review sample — 2026-10-27

This is an owner review record. On 2026-10-27, the owner explicitly approved
the exact revisions of `structured-logging.md` and `design-for-failure.md`
after inspecting their claims, quotes, and source limitations. The
`controlled-refinery-change-validation.md` source quote was corrected at the
owner's direction but remains pending. A product-target draft was captured as
pending after the owner authorized a local source copy. The sodium-ion claim
remains unapproved.

The original read-only audit across 13 atoms found six exact anchors. After the
owner-approved evidence correction, `controlled-refinery-change-validation.md`
also resolves uniquely to `0-raw/archive/2026-07/cognitive-core-validation.md`
(bytes 248–399; hash
`3c804d81b48e9d91a02776e32f50afb4ee9fd083ff75781e99c9bc20acd9cf9f`). The new
`owner-product-target.md` draft anchors to its captured owner source at bytes
260–778. Other Cognitive Core atoms, the refinery entrypoint atom, and
sodium-ion still fail exact resolution. An exact anchor only proves that the
excerpt occurs in a particular local file; it does not prove authority or claim
truth.

## `structured-logging.md`

- Atom claim: “Logs should be structured (JSON) to facilitate observability and traceability.”
- Named source resolves uniquely to `0-raw/archive/2026-07/high-level-engineering-practices.md`.
- The quoted evidence occurs in that source at byte range 1398–1525 under SHA-256 `c92f859a94e925f91a80911ddb77b8256086bb62a885356c945498fcef2069f6`.
- The excerpt says structured JSON logs should contain request IDs for tracing and avoid logging PII. It supports the atom’s general recommendation.
- Limitation: the stored source is a local capture with no author or publication metadata. Exact quotation and local integrity do not establish the underlying guidance’s authority or the owner’s endorsement.
- Review result: the owner approved this exact atom revision. The canonical mutator recorded actor `camer`, the atom hash, and the evidence anchor in `0-raw/archive/decision-record.jsonl`. The approved claim is the general structured-logging recommendation; the source is a local capture without author metadata, so the approval does not upgrade the source's authority.

## `sodium-ion-battery-cost-record.md`

- Atom claim: sodium-ion battery packs have reached `$56/kWh` in a Chinese EV.
- Named source is `example-capture-sodium-ion.md`, archived under `0-raw/archive/2026-07/`.
- The capture is a user note that attributes the statement to `https://example.com/sodium-ion-56kwh-2026` and explicitly says “if this holds” and to check against earlier battery cost curves.
- No article or other primary evidence for the price claim is present in `sources/` or the raw archive. The atom’s excerpt does not anchor to the named file under the current exact-quote validator.
- Review result: do not approve. The captured note documents an unverified lead, not evidence that the price claim is true.

## `design-for-failure.md`

- Atom claim: “High-level systems should be designed to handle failures
  gracefully.”
- Supporting excerpt: “Assume the network will fail, the database will go
  down, and the API you depend on will timeout. Implement retries, exponential
  backoffs, and circuit breakers.”
- Named source resolves uniquely to
  `0-raw/archive/2026-07/high-level-engineering-practices.md`, bytes
  1044–1203, SHA-256
  `c92f859a94e925f91a80911ddb77b8256086bb62a885356c945498fcef2069f6`.
- Limitation: this is an unattributed local capture; the anchor proves excerpt
  integrity, not external authority.
- Review result: the owner approved this exact atom revision. The canonical
  mutator recorded actor `camer`, the before/after atom hashes, source anchor,
  backup path, and decision timestamp `2026-10-27T18:17:10`.

## `controlled-refinery-change-validation.md`

- Review result: the owner authorized replacing the malformed Evidence text
  with the exact source excerpt and setting `epistemic_label: procedure`.
- The mutation went through the canonical CAS mutator, created a revision
  backup, refreshed the lexical index, and recorded both edit hashes. The
  revised atom remains `review_status: pending` and `citation_integrity:
  missing`; no approval was inferred. It is ready for a separate owner review.

## `owner-product-target.md`

- The owner authorized a local source copy of the existing product target in
  `CODEX_CLI_GOAL.md` and a pending review draft.
- The new source records the original document path and SHA-256. The episodic
  draft is source-anchored, project-scoped, and remains `claim_state: candidate`
  / `review_status: pending`. This is recorded as a project decision, not an
  approved preference or a claim that the product has been delivered.

## Effect on retrieval

`structured-logging.md` and `design-for-failure.md` have current owner approval.
The corrected procedure and product-target draft remain pending. Sodium-ion
remains unapproved. Pending atoms must remain unavailable to standard factual
recall. Dataset v2.0.0 pins these two approved revisions and keeps pending
candidate labels out of answerability scores. Its retrieval results still do
not measure generated-answer grounding.
