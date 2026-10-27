# Second Brain Cognitive Core

## Status

Draft architecture note. This document describes the direction for the next
major capability layer on top of the existing Second Brain infrastructure.

## Purpose

Second Brain already has the local infrastructure for knowledge capture and
retrieval: Markdown vault, embeddings, Graph RAG, MCP access, dashboard, web
scout, and watchdog ingestion. The Cognitive Core is the layer that turns that
infrastructure into a learning loop.

The core idea is:

```text
Experience -> Extract -> Split -> Remember -> Reflect -> Learn -> Improve -> Practice
```

Memory is not the destination. Memory is one stage in a cycle that improves
future retrieval, procedures, and decisions.

## Layers

```text
Layer 5: Executive intelligence
Layer 4: Skills
Layer 3: Memory
Layer 2: Knowledge
Layer 1: Infrastructure
```

Layer 1 and Layer 2 are mostly present today. The next focus is Layer 3:
structured, typed, measurable memory.

## Memory Types

Every durable memory remains a Markdown file with YAML frontmatter. Markdown is
the human-readable source of truth. Embeddings provide semantic search. Later,
a small operational database can hold fast-changing statistics.

Allowed `memory_type` values:

- `semantic`: Facts, claims, concepts, definitions, principles, and reference knowledge.
- `episodic`: Events, observations, user interactions, decisions, and things that happened.
- `procedural`: Repeatable workflows, methods, playbooks, checklists, and task steps.
- `self`: Facts about the local AI system, tools, constraints, configuration, and capabilities.

## Base Memory Schema

```yaml
id:
type: atom
memory_type: semantic
certainty: tentative
created:
last-reviewed:
importance: 0.50
confidence: 0.50
sources: []
links: []
tags: []
contexts: []
supersedes: []
superseded_by: []
contested_by: []
```

`utility`, retrieval counts, success counts, failure counts, and last-accessed
timestamps should eventually live outside Markdown to avoid noisy Git churn.

## Context Profiles

Memories should not be ranked by semantic similarity alone. Retrieval should
eventually combine:

- Semantic similarity.
- Utility.
- Freshness.
- Accuracy or confidence.
- Context match.

The `contexts` field is the durable human-readable profile. Example contexts:

- `windows`
- `linux`
- `coding`
- `research`
- `beginner`
- `expert`
- `offline`
- `cloud`
- `gpu`
- `cpu`

## Memory Lifecycle

The intended lifecycle is:

```text
Capture -> Extract -> Split -> Categorize -> Validate -> Store -> Link -> Reflect -> Score -> Consolidate -> Archive
```

The first implementation slice changes the Refinery from one source producing
one atom to one source producing many typed memory atoms.

## Experience Records

The Learning Engine should record important completed tasks as experiences:

```yaml
experience_id:
goal:
context:
plan:
actions:
tools_used:
reasoning_summary:
result:
success:
confidence_before:
confidence_after:
new_memories:
updated_memories:
candidate_skills:
reflection:
lessons:
```

Experience records should suggest memory or skill updates first. They should
not silently rewrite trusted memories.

## Skill Promotion

Procedural memories should mature through explicit stages:

```text
Procedural Memory -> Verified Procedure -> Skill Candidate -> Executable Skill
```

Promotion should depend on repeated successful use, low correction rate, clear
inputs, clear outputs, and human-readable recovery steps. Executable skills are
not part of the current slice.

## What Not To Build Yet

- No self-modifying code pipeline.
- No continual model fine-tuning.
- No distributed CRDT synchronization.
- No autonomous deletion of memories.
- No executive planner until typed memory and reflection records are reliable.

## Next Milestones

1. Multi-memory typed Refinery.
2. Operational stats store for retrieval and utility metrics.
3. Memory fitness reports.
4. Experience and reflection records.
5. Procedure verification and skill-candidate tracking.
6. Executive planner using mature skills and memory, not raw notes.
