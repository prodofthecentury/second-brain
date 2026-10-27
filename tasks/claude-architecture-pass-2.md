# External Architecture Review — Pass 2

Use your Pass-1 repository findings, but challenge this primary-architect response rather than agreeing with it.

## Facts independently confirmed

The silent 256-dimensional hash fallback, all-atom re-embedding, `0.0` score on mismatched vector lengths, pending atoms in default recall, scout unrestricted fetches, MCP write/full enumeration, and destructive thread overwrite are present. The current machine is a 16 GB-RAM Windows laptop with a GTX 965M (2 GB VRAM), i7-6700HQ (4C/8T), Ollama CLI 0.32.4 installed but no running server or installed models. It cannot truthfully be described as simultaneously frontier-smart and fast without a hardware tier change. Ollama's current documentation lists the GTX 960/965M generation as compute capability 5.2, subject to its supported driver requirement; current installed driver is 399.01-era, not the required 531+.

## Proposed response to Pass 1

We agree on a *fail-closed interpretation and indexing contract*: raw capture/archive may succeed without models, but extraction, embedding, critic, synthesis, and normal recall must not counterfeit semantic capability when their model is unavailable. `pending` content is excluded from normal factual answers; a deliberately selected Research/Explore mode may surface it only as untrusted leads.

We propose five durable planes, while retaining Markdown as canonical human-readable memory:

1. **Immutable evidence plane:** content-addressed raw artefacts plus a manifest containing capture ID, SHA-256, original source/URL, custody events, trust origin, and chunk anchors. No extraction overwrites evidence.
2. **Canonical memory plane:** versioned Markdown atoms with typed citations pointing to evidence anchors. Distinct fields for claim epistemic status, extraction confidence, source quality, review state, author/model, and derivation; free tags are separate from a closed retrieval-context vocabulary.
3. **Operational sidecar:** local SQLite/WAL only for rebuildable job state, locks, review actions, idempotency keys, telemetry, lexical FTS, and vector-index catalogue. It is never the sole record of a user memory; it can be rebuilt from Markdown/manifests. One job worker owns mutations.
4. **Derived retrieval plane:** hybrid lexical+dense retrieval, explicit embedding model/dimension/content hash, candidate fusion, optional local reranker, then a strict context budgeter. Typed graph edges are used only for provenance/lifecycle effects first; multi-hop semantic expansion remains disabled until evaluation proves it improves answers.
5. **Capability plane:** an explicit model profile declares the available generation/embedding/vision/rerank capabilities, context limits, latency/VRAM budget, and version. The core becomes functional in capture/review/search mode on this laptop; extraction/synthesis require an intentionally installed model profile. Future hardware can opt into a much stronger profile without changing data contracts.

MCP becomes local-only, scoped, explicit-consent, and read-only by default. Any cloud host gets no automatic vault enumeration and must receive only user-approved, redacted query results. Web/remote/MCP input enters a labelled quarantine with network allow/deny controls, byte limits, and a separate promotion path. Synthesis threads become either machine-owned derived views or human-owned notes; never both.

## Pass-2 task

Act as a hostile staff/principal architect. Find concrete flaws, needless complexity, missing invariants, migration traps, and performance bottlenecks in this proposal. In particular:

- Is a rebuildable SQLite sidecar the smallest acceptable operational spine, or does it introduce a dual-truth problem worse than the current architecture? Name the exact boundary.
- Is excluding pending content from normal recall too conservative for an advanced personal second brain? Define a safe, useful mode model if so.
- Which parts can actually be efficient on the described laptop, and which should be made optional/deferred instead of merely configurable?
- Specify an evaluation harness that can prove the new retrieval and trust model outperform the current behavior without relying on subjective demos.
- Define a migration order that preserves existing atoms and raw captures even if interrupted midway.
- State the 10 non-negotiable invariants and the smallest technical checks that make each observable.

Do not write code or modify files. Return substantive objections first, then a revised architecture recommendation and phased plan. Do not praise the proposal.
