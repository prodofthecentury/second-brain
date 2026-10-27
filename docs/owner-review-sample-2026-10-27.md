# Evidence review sample — 2026-10-27

This is an owner review record. The owner explicitly authorized approval of `structured-logging.md` in chat on 2026-10-27 after inspecting this audit. The sodium-ion claim remains unapproved.

A read-only anchor audit across all 13 atom files found six excerpts that can be anchored to their named raw archive and seven that fail exact source resolution. The six technically anchorable files are `code-reviews-as-mentorship.md`, `continuous-integration-deployment.md`, `design-for-failure.md`, `observability-o11y.md`, `structured-logging.md`, and `tdd-and-automated-testing.md`. The other seven include legacy Cognitive Core references to a missing `cognitive-core-validation.md`, plus the sodium-ion capture; they remain unapprovable until their source references/evidence are repaired. An exact anchor only proves that the excerpt occurs in a particular local file; it does not prove authority or claim truth.

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

## Effect on retrieval

The structured-logging atom now has a current owner approval; the sodium-ion atom remains pending and unapproved. Recall should return only the former where relevant and continue to suppress the latter.
