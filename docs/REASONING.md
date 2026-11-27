# Reasoning engine

Deep reasoning is the default for chat and for the `reason` MCP tool. The committed default is `deep` in `config/reasoning.json`. A caller opts out of the deep chat path with profile `fast`. On the `reason` tool, `fast` still uses the engine and only shrinks the step, model-call, and retry budget.

## Latency

A 3B model on CPU often spends several seconds to a minute on one generation. A deep answer can take several times a single reply when the question needs the model. Arithmetic, dates, and unit conversions are computed by tools and do not call a model. Chat profile `fast` is the previous single-pass grounded reply.

This is stage 1. It does not yet sample several drafts, search a tree of plans, store worked examples, or route hard steps to a larger remote model. Those are later stages. The numbers below are only as good as the run that produced them.

## What stage 1 does

1. Classify the question.
2. If it is arithmetic, a date offset, or a unit conversion, compute it and recompute it. The calculator uses SymPy when that package is installed, and a restricted arithmetic evaluator otherwise.
3. If the caller supplied tests, ask the model for a function and run the tests in the existing AST sandbox (timeout, no network, temporary directory). One retry on deep. Then abstain.
4. Otherwise, when a model is allowed, retrieve memory, draft an answer, and reject a draft that introduces a number the memory does not contain.
5. Chat in `stream` or `verified` mode uses that tool path first. A question the tools do not solve is handed to the existing grounded chat path, and the hand-off is recorded on the `done` event. Debate, research, deliberative, cognitive, and instant modes keep their existing executors and are marked the same way. Profile `fast` skips the engine.

## Later stages

Each stage is its own pull request and keeps a benchmark table.

2. Self-consistency, verified voting, critique and repair, abstention calibration.
3. Tree or beam search over plans, with verifier scores and backtracking.
4. Store solved traces and retrieve them for similar problems. Playbooks grow from successes and failures.
5. Small local model for ordinary steps, a larger remote or desktop model for hard steps, with a cost and latency budget.
6. A broader harness: math, multi-hop notes, coding with tests, planning, and logic puzzles, tracked per model against the plain baseline.

Knowledge packs stay the next product priority after stage 1 or 2.
