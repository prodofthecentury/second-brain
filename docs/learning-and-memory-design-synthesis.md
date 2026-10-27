# Learning and Memory Design Synthesis

**Status:** Proposed design contract. It sharpens the architecture draft; it does not approve model training, new network access, or changes to the owner-review boundary.

**Date:** 2026-07-27

**Scope:** This is a study memo for the offline local Second Brain. It is not a canonical memory atom and is not treated as a factual source for user-facing answers. It records what the supplied material means for the system design, including its limits and disagreements.

## Source register and study method

I read the supplied material in full where feasible, sampled the 308-page mathematical text at the chapter and key-mechanism level, and visually inspected the architecture/equation pages in the three PDFs. The raw sources remain outside the vault and have **not** been ingested, promoted, or added to the raw queue.

| Source | Role in the study | SHA-256 |
| --- | --- | --- |
| `C:\Users\camer\Downloads\New folder\2603.18387v1.pdf` - *Mathematical Foundations of Deep Learning: Theory and Algorithms* (Ye, 2026), 308 pp. | Theoretical framing for function classes, non-convex optimisation, autodiff, control, RL, and generative models. | `66D11251192FACA92F703DA23D7D04ECA7FCF95AECC491620014655285ECE7E6` |
| `C:\Users\camer\Downloads\New folder\NIPS-2017-attention-is-all-you-need-Paper.pdf` - Vaswani et al., 2017, 11 pp. | Primary source for the original Transformer architecture and attention equation. | `D87D482D5AE7960E2E43D7DD6D21377E60E73E8FCE1BF2A01AFF7ACA8A08C537` |
| `C:\Users\camer\Downloads\New folder\ai human.pdf` - Zhao et al., 2023, 11 pp. | Brain-inspired-AI/AGI survey and design inspiration, with dated claims called out below. | `6B63AEA941569958378ABD9B6E474F4EB9E3B81EBD00851CFB429CA4FF303FFE` |
| `C:\Users\camer\.codex\attachments\f05b3652-567f-46cd-8555-96fc8e626ac0\pasted-text.txt` - *The bigger picture*. | Detailed brain/AI comparison centred on prediction, world models, attention, and expert selection. | `D4350CF75A0A0343C8337585A5C1DFE82C7CB5D7B57FF6E22425B9BD47C57946` |
| `C:\Users\camer\.codex\attachments\23b7b93d-0c32-49d5-af19-8556501688b7\pasted-text.txt` - Transformer tutorial. | Pedagogical explanation and illustrative NumPy sketch. Secondary to the original paper. | `0E44B9348180CAFEA48519F37AACE358FAD504D3CC372B1092E0F8955F5174C4` |
| `C:\Users\camer\.codex\attachments\99993b6d-fc6f-41b5-b1ea-608688810d7a\pasted-text.txt` - PPO explainer. | Accessible description of policy gradients, PPO clipping, GAE, and RLHF. | `BB04D1A2A10F755B78FF2A55FD7ADE4F382D281D0D80DBFBDFEDE1A82BBCED17` |

The source hashes identify precisely what was studied. They do not validate every claim made by a source. The present project’s evidence pipeline remains the only route by which any external material becomes a retrievable, trusted memory.

## The central distinction: four different things people call “learning”

The project must never collapse these into one vague promise of a “self-learning AI.”

| Mechanism | What changes | Timescale | Appropriate for this project? |
| --- | --- | --- | --- |
| Pre-training or fine-tuning | Model parameters/weights | Hours to months; requires curated data and an evaluation regime | No autonomous use. The current machine is unsuitable for responsible continual training. |
| In-context learning | The active prompt/context, not weights | One conversation or task | Yes. Build explicit, bounded working context. |
| Memory learning | Durable evidence, atoms, indexes, links, and owner decisions | Persistent, reversible/auditable | Yes. This is the core product. |
| Policy learning | The choice of actions/workflow behaviour | Only after measured feedback and a safe objective | Constrained automation only. Never self-rewarding or self-authorising. |

A strong local Second Brain is therefore **a controlled memory-and-context system around a fixed local model**, not a claim that the model continually rewires itself. Its intelligence comes from better retrieval, evidence, task framing, and safe feedback loops.

## What the Transformer sources establish

The original paper’s useful technical core is precise:

- Attention produces a weighted value mixture: `softmax(QK^T / sqrt(d_k))V`. The scale prevents dot products from saturating softmax and damaging gradients during training.
- Multi-head attention gives multiple projected query/key/value subspaces; it is not multiple independent truth checkers.
- An original encoder-decoder Transformer separates encoded source context from causal, masked generation. Decoder masking prevents using future output tokens. Residual paths, layer normalisation, feed-forward transformations, and positional information are structural parts of the design.
- Self-attention has quadratic sequence cost in sequence length. A long, indiscriminately assembled prompt is therefore both slower and less reliable on this laptop.

**Design consequence:** retrieval is analogous to an external, explicit attention-selection stage. A query planner chooses a small evidence-backed set of atoms; the model reasons over that set; the answer assembly layer attaches sources and trust markers. Similarity ranking is useful for selection, but neither cosine similarity nor attention weight is truth, endorsement, or permission to write memory.

The tutorial correctly explains the intuition of Q/K/V, masking, multi-head projections, feed-forward blocks, residual connections, normalisation, and positional information. Its NumPy sketch is deliberately simplified, however: the shown layer implementations do not actually implement the residual/normalisation described in the prose, and the same `mask` is reused where production decoder self-attention and cross-attention need different masking semantics. It is good for intuition, not an implementation specification.

## What the mathematical foundations text establishes

The book separates problems that casual AI discussions often blur together:

- A model class determines what can be represented; an objective says what is rewarded; an optimisation algorithm says how parameters move; data and evaluation determine whether the result generalises. Universal approximation does **not** imply efficient learning, adequate data, robustness, faithfulness, or truth.
- Neural-network training is normally non-convex. A lower apparent objective is not proof of a globally better system, and a local improvement can be a harmful policy change.
- Automatic differentiation and reverse-mode computation explain how modern models are trained; they do not make a small local runtime capable of safe online training.
- The RL chapters distinguish Markov state, actions, transition dynamics, rewards, policy/value/Q-functions, Bellman backup, Monte Carlo, and temporal-difference updates. That distinction is valuable for designing a controller, even though this system is not an RL environment.
- The control and generative-model chapters reinforce that prediction, optimisation, and distribution modelling are separate concerns. A predictive model can be useful without being a reliable factual database.

**Design consequence:** every capability proposal needs an explicit tuple: `(state observed, permitted action, objective, data/evidence boundary, evaluator, rollback)`. “Make it smarter” is not an objective. For example, a retrieval upgrade must name a frozen query set, expected citations, trap queries, latency target, acceptance metric, and atomic rollback to the previous index generation.

## What PPO and RLHF contribute - and why they are a warning

PPO is an on-policy actor-critic method. It adjusts a policy to improve estimated return while bounding the probability ratio between the new and old policy with a clipped surrogate objective. GAE trades bias against variance when estimating an action’s advantage. Its point is not that small steps are always safe; it is that unconstrained updates can collapse a policy and that an update only has meaning relative to a specified reward, environment, and data distribution.

RLHF applies preference data to training, not to magical real-time model self-improvement. It can reduce some unwanted behaviour, but it does not prove facts, remove reward hacking, or supply a universal definition of user values.

**Decision:** this Second Brain will not use PPO, RLHF, self-generated rewards, or autonomous parameter updates. It does not have a stable simulator, a defensible reward function, or a sufficiently broad and independent evaluation set. The safe analogue of PPO’s “stay near a known-good policy” is a **change gate**:

1. propose a narrow, reversible workflow/index/configuration change;
2. run it in a staged generation against frozen evaluations and adversarial trap cases;
3. compare quality, provenance correctness, failure rate, latency, and resource cost with the active generation;
4. activate atomically only after the owner accepts the evidence;
5. retain the previous generation for rollback.

This is not PPO. It borrows the disciplined idea of bounded, measured change without pretending a preference reward is trustworthy.

## What the brain-inspired sources contribute - and their limits

The longer comparison proposes four algorithm-level parallels: prediction-error learning, reuse of predictive models for comprehension and generation, hierarchical attention, and adaptive switching among specialised modules. It distinguishes these high-level parallels from implementation-level differences: brains are recurrent, heterogeneous and embodied; standard Transformers are not. It also describes fast/coarse versus slower/contextual processing as a useful analogy.

The 2023 survey adds useful prompts: multimodal inputs should retain their modality and provenance; alignment needs human feedback and governance; infrastructure and resource limits are design constraints, not afterthoughts; parameter count alone does not determine competence.

Those ideas are inspiration and hypotheses, not a licence to call the project a brain or AGI. In particular:

- The papers make broad, sometimes speculative brain/AI analogies. A correlation or a shared vocabulary such as “attention” does not establish a shared mechanism.
- The 2023 paper is dated. Its statement that GPT-4 has 10 trillion parameters is not established by the cited public technical report and must not be used as a project fact.
- “World model” must not mean an untraceable, autonomous internal database. Here it means an explicit, inspectable working representation assembled from evidence and user-approved memory.

## Proposed cognitive architecture for this project

```text
User question / active task
        |
        v
Task interpreter (no authority to write or promote)
        |
        +--> working context: conversation, task state, short-lived scratchpad
        |
        +--> memory retrieval: lexical first, verified dense optional
                 |
                 +--> immutable evidence + canonical atoms + provenance/lifecycle filter
        |
        v
Answer/plan assembler with structural citations and trust view
        |
        +--> proposal only: capture, draft, link, or review recommendation
        |
        v
Owner-approved canonical mutation -> staged sidecar rebuild/evaluation -> atomic activation
```

### Memory roles, deliberately not one bucket

| Memory role | Durable representation | Retrieval/review rule |
| --- | --- | --- |
| Evidence/source memory | Immutable bytes, hashes, offsets, custody | Never altered; may be cited or quarantined. |
| Episodic/task memory | Timestamped events, decision records, task status | Describes what happened; does not become a general fact by itself. |
| Semantic memory | Canonical Markdown atoms | Normal factual view requires `human_reviewed`; source-anchored material is visibly separate. |
| Procedural memory | Versioned workflows, capability profile, tests, operational notes | Runs only through explicit, bounded commands and capability gates. |
| Working memory | Active conversation and task-scoped scratchpad | Ephemeral; no silent promotion to durable memory. |

This separation supplies a useful “second brain” without giving the system permission to turn every conversation, model thought, or external webpage into a fact.

### Learning cycle

```text
Capture -> provenance stamp -> quarantine/evidence preservation
       -> model draft (optional, bounded) -> deterministic validation
       -> owner review -> canonical atom/decision record
       -> staged lexical/vector sidecar -> frozen evaluation
       -> atomic activation or rollback
```

The system may recognise patterns, propose links, and prepare a review queue. It may not use its own outputs as unexamined training labels, alter model weights, promote a claim, resolve a contradiction, delete memories, or expand its authority.

## Immediate changes to the existing architecture plan

The architecture draft already has the correct foundations: immutable evidence, canonical atoms, rebuildable sidecars, structural trust states, owner-only review, lexical-first retrieval, and capability gates. This study adds the following requirements before calling the system “advanced”:

1. **Versioned capability-and-learning contract.** Record the fixed model identity, context limits, embedding model/dimensions, enabled actions, resource limits, evaluation version, and explicit statement that parameter learning is disabled.
2. **Gold-set evaluation before retrieval sophistication.** Add owner-labelled queries for exact lookup, paraphrase, conflict/supersession, time-sensitive material, and trap queries that can only be answered by candidate, quarantined, or stale information. Score attribution correctness separately from relevance.
3. **A bounded context assembler.** Give the model an explicit token budget and evidence cap. Prefer a small, diverse, provenance-valid set over dumping the vault into a prompt. Preserve query, retrieved IDs, index generation, and final citations as an auditable episode.
4. **Separate trust views and action permissions.** Standard view may use only reviewed direct assertions. Research view can reveal leads, never silently promote them. Write/mutation paths remain owner-gated regardless of view.
5. **No fake “fast learner” claim.** Fast adaptation means changing task context and memory state under audit, not pretending that preference feedback immediately improves the base model.
6. **Multimodality only with evidence parity.** A PDF, image, audio clip, or screenshot must receive immutable source bytes, extraction/version metadata, page/time coordinates, and a user-visible limitation before it participates in factual retrieval. Vision should remain disabled until the runtime actually proves it works on this hardware.
7. **Module routing without autonomous agents.** “Expert modules” initially mean explicit tools: capture, evidence validator, lexical search, dense retrieval, review mutator, and answer assembler. Each has a narrow input/output contract and no implicit authority.

## Hard constraints and non-goals

- Current hardware (16 GB RAM, i7-6700HQ, GTX 965M 2 GB) supports a small local generation model, model-free retrieval, and cautiously measured embedding. It does not justify a heavy default model, always-on multi-agent loop, vision pipeline, continual training, or real-time reranking claim.
- A model’s fluent explanation, attention pattern, confidence, or self-critique is not evidence of truth.
- Rebuilding an index is not learning in the parameter sense. It is a deterministic derived-data projection and must be reproducible from durable sources.
- The system cannot safely infer a human’s values from clicks, use frequency, silence, or model-generated feedback. Promotion, deletion, policy changes, and network policy remain owner decisions.
- A future fine-tuning experiment would need a separate owner-approved ADR, isolated data, redaction policy, reproducible training recipe, holdout evaluation, model provenance, rollback plan, and hardware/cost assessment.

## Next implementation order

1. Finish the active reliability slices: dashboard verification, immutable evidence linkage/offset validation, lexical FTS, research/standard rendering, and external-input hardening.
2. Add the capability-and-learning contract and frozen evaluation corpus before enabling dense retrieval as a normal capability.
3. Implement the bounded context assembler with an auditable episode record.
4. Measure local latency, quality, and failure modes; only then decide whether a small reranker, multimodal extractor, or additional model has a net benefit.
5. Treat any later autonomous action or model-training proposal as a new, reviewable architectural decision.

## Re-check questions for every future feature

1. What exact state changes, and is the previous state recoverable?
2. What evidence supports the result, and can the user inspect it?
3. What trust state and origin may enter the output or action?
4. What capability has been measured on the actual machine rather than assumed?
5. What frozen test, trap case, latency target, and rollback prove this is better?
6. Does this feature accidentally turn a model’s suggestion, an external source, or a transient prompt into an authoritative memory?

