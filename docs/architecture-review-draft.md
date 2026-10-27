# Advanced Offline Second Brain — Architecture Review Draft

**Status:** Proposed — requires owner approval before implementation.

**Date:** 2026-07-26

## Purpose

Turn the current feature-rich local vault into a trustworthy, fast, offline-capable personal intelligence system. The immediate goal is not more autonomous behavior. It is a memory system that never silently pretends degraded, unreviewed, or externally supplied information is reliable.

## Review record

This draft was developed through three read-only Claude Opus architecture passes, each challenged by a primary-architect response, followed by separate local and external system sweeps. The review prompts are retained under `tasks/claude-architecture-pass-*.md` and `tasks/claude-system-sweep.md`.

The reviews changed the direction materially:

1. Initial concern: durable Markdown and rebuildable indexes are sound, but the system silently substitutes hash embeddings and accepts unreviewed memories into normal answers.
2. First proposal: five architecture planes with a SQLite operational sidecar.
3. Adversarial correction: reduce this to three stores; SQLite must contain no authoritative state. Use lexical search first on the actual hardware.
4. Final correction: direct human edits are canonical mutations. They must re-run citation checks and demote the edited atom until re-affirmed; simply rebuilding its index would make the trust labels false.

## Observed constraints

- Hardware: 16 GB RAM, Intel i7-6700HQ (4C/8T), Nvidia GTX 965M (2 GB VRAM).
- Local runtime: Ollama is installed; direct probes succeeded with `llama3.2:3b` and `nomic-embed-text` (768 dimensions).
- A 23 GB Qwen model is installed but must not be treated as usable merely because it appears in `ollama list`; capability is a runtime fact, not a configuration claim.
- The current corpus is small: 13 atoms, 23 raw captures, and 14 queued URLs.
- The system must remain useful when no generation model is available.

## Non-negotiable principles

1. **Capture may degrade; interpretation may not counterfeit success.** Archiving a new capture can succeed without models. Extraction, embedding, synthesis, or criticism must defer visibly when their required model is unavailable.
2. **Markdown and evidence files are durable; indexes are disposable.** Deleting the sidecar must lose neither evidence nor a human decision.
3. **No fabricated embedding may enter a served index.** Each vector belongs to one named index generation with its embedding model, vector dimension, and source-content hash.
4. **Trust is not model self-confidence.** Models do not set certainty or confidence scores. The system records deterministic provenance and integrity; the owner makes entailment and review decisions.
5. **External input remains externally sourced.** Web and MCP provenance is sticky. External data cannot promote itself into a normal answer.
6. **Normal answers are structurally constrained.** A model-generated disclaimer is not a security control. Trust markers and provenance must be added outside the model's prose.
7. **Every edit can invalidate trust.** Changing an atom's claim or evidence is a canonical event, not an index refresh.
8. **No autonomous deletion or contradiction adjudication.** The system may flag, archive, and propose; the owner decides deletion, merging, supersession, and promotion.

## Final target architecture

```text
Local capture / existing files / approved imports
        |
        v
Immutable evidence store + append-only decision record
  - SHA-256, origin, custody, source path, byte offsets
  - raw capture is preserved before interpretation
        |
        v
Canonical Markdown atoms
  - human-readable memory and current state
  - atomic replacement only through the canonical mutator
  - external edits are detected, compare-and-swap applied, then re-gated
        |
        v
Rebuildable derived sidecar
  - SQLite FTS5, vector catalog/generations, telemetry
  - never authoritative; deletable and reproducible from durable files
        |
        v
Shared retrieval and answer assembly
  - lexical first; dense retrieval only after its own measured generation passes
  - provenance/lifecycle effects before semantic graph expansion
  - Standard and Research views, with structural trust markers
```

### Store boundaries

| Store | Authoritative data | May be rebuilt? | Writer policy |
| --- | --- | --- | --- |
| Immutable evidence | raw bytes, hashes, origin, custody, decision records | No | append-only capture; owner decisions only |
| Markdown atoms | memory text and current human-readable state | No | one OS-enforced canonical mutator; atomic replace + compare-and-swap |
| SQLite sidecar | FTS, embedding catalog, index generations, telemetry | Yes | projection worker only |

SQLite is intentionally deferred from holding queues, locks, review decisions, or any other authoritative state. A full event ledger is also deferred from v1: per-atom atomic writes plus backups and a flat decision record are sufficient until the project needs a true multi-atom transaction such as an atomic merge/supersession.

### Integrity and state model

Each memory has these separate, non-overloaded properties:

- `origin`: `user_local`, `mcp_local`, `web_remote`, or `imported_unknown`; system-stamped and sticky.
- `citation_integrity`: `anchored`, `missing`, `hash_mismatch`, or `invalid_offset`; deterministic validation against immutable source bytes.
- `claim_state`: `candidate`, `source_anchored`, `human_reviewed`, `superseded`, `rejected`, or `quarantined`.
- `epistemic_label`: owner-set `observation`, `preference`, `hypothesis`, `claim`, or `procedure`.

An anchored quote only proves that the cited bytes still exist; it does not prove the quote entails the claim. Only the owner may move a claim to `human_reviewed`. `hypothesis` and `preference` are barred from being presented as factual assertions even if their citations remain anchored.

### Retrieval and answers

- **Standard view:** direct assertions can use `human_reviewed` content. `source_anchored` material is rendered in a separated, non-model evidence area with provenance; it is never silently restated as a fact. Candidates and quarantined material do not become factual prose.
- **Research view:** adds candidates and quarantined items as visibly labelled leads. It never promotes them or turns them into facts.
- **Lexical FTS5 is first:** it is cheap and model-free on the current laptop. Dense retrieval is introduced only if the evaluation harness improves retrieval without missing its latency target.
- **Graph features are narrow initially:** lifecycle/provenance links such as `superseded_by` and `contested_by` affect ranking and display. Semantic multi-hop expansion, automatic threads, and reranking are deferred until measured benefit justifies them.

### Capability profile

Capabilities are probed at runtime, not declared optimistically. A usable profile must prove model availability, embedding dimension, and measured behavior. A model or embedding upgrade creates a new index generation; the system builds it separately, evaluates it, then atomically selects it. Cross-generation vectors are never compared.

On the current laptop:

- Available now: capture, evidence hashing, audit, review, lexical search, citation integrity, small explicit batch generation, and small-batch dense retrieval when measured.
- Explicitly absent until a future capability profile enables and proves them: vision, cross-encoder reranking, semantic multi-hop expansion, automatic synthesis threads, real-time extraction, and 7B+ default generation.

## Seven implementation slices

1. **Truthful runtime baseline:** repair the dashboard launcher, add capability/index-generation health to `doctor`, and remove the served hash-embedding path.
2. **Immutable evidence:** create non-destructive manifests with hashes, origin, custody, and resolvable source links.
3. **Model-free lexical retrieval:** add FTS5 behind the shared recall contract and establish the frozen gold set.
4. **Citation integrity:** add byte-offset/quote validation, claim states, and automatic re-gating on edits.
5. **Safe owner review:** implement review transitions with an OS singleton, atomic replace, compare-and-swap, and a flat decision record.
6. **Structural trust assembly:** implement Standard/Research behavior, model-external provenance chrome, and deterministic trap-query checks.
7. **Safe automation:** quarantine Scout/MCP input, scope MCP to explicit local consent, and make small-model drafting explicit, cancellable, and bounded.

Each slice must leave the system functional and pass its own tests before the next one begins. Dense indexing is a separately measured follow-on, not a prerequisite for the first trusted release.

## Evaluation and release gate

Create a small, owner-labelled, versioned gold set covering exact lookup, paraphrase, multi-atom queries, conflicts, supersession, and trap queries whose only apparent answer is unreviewed or quarantined. Measure Recall@k, MRR/nDCG, groundedness, attribution correctness, fabrication rate, and p50/p95 latency on this laptop.

Release requires all of the following:

- no non-real or foreign-dimension vector in the served generation;
- all evidence hashes, source links, and citation labels validate;
- trap-query factual fabrication rate is zero;
- unavailable capabilities cannot be invoked;
- no Scout/MCP content leaves quarantine without an owner action;
- each failure-injection case in `docs/system-sweep-2026-07-26.md` passes.

## Owner-only decisions

- Human review, entailment, epistemic labels, promotion, rejection, supersession, and merge decisions.
- Evidence or atom deletion.
- Network allow/deny policy, MCP connection to a non-local host, and vault-redaction policy.
- Enabling or replacing model/index generations and accepting a schema migration.
- Any override of a citation-integrity failure.

## Explicitly not in v1

- Self-modifying code or autonomous skill execution.
- Continual fine-tuning.
- Autonomous deletion, merge, or conflict adjudication.
- Real-time LLM extraction, 3D graph-first workflows, automatic thread writing, vision, or a heavy reranker on the current hardware.
- A claim that the system has perfect memory or that every source-anchored note is true.

