# Second Brain Cognitive Core

## Status

**Implemented (Phases 2, 3A & Stage 4 Executable Skills).** This note originally specified the Cognitive
Core architecture layer on top of Second Brain. All Cognitive Core
milestones—including Layers 4–5 (fitness-aware ranking, owner-gated Skill
Playbooks in `brain/5-playbooks/`, Stage 4 Sandboxed Executable Skills in
`brain/6-skills/` via `scripts/executable_skills.py`, and the grounded Executive
Planner in `scripts/executive_planner.py`)—are now implemented, security-audited,
and verified. See `docs/phase2-architecture-and-sweep-2026-11-15.md` and
`docs/phase3a-and-phase4-architecture-2026-11-15.md` for architecture
details and security invariants.

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

Promotion depends on repeated successful use, low correction rate, clear
inputs, clear outputs, and human-readable recovery steps. Stage 4 Executable
Skills (`brain/6-skills/`, `scripts/executable_skills.py`, `second-brain skills`,
`/v1/skills/*`) promote active verified Playbooks via owner CAS into a
closed-catalog network-jailed sandbox runtime with 25ms live kill-switch
polling (`skill-kill-switch.json`), automatic workspace snapshot rollback on
failure, and a hash-chained audit trail (`brain/0-raw/archive/skill-executions.jsonl`).

## Guardrails (Still In Force)

- No self-modifying code pipeline.
- No continual model fine-tuning.
- No distributed CRDT synchronization.
- No autonomous deletion of memories.
- No autonomous Playbook promotion or edits outside owner CAS + SHA-256 audit logs (`brain/0-raw/archive/playbook-decisions.jsonl`).
- Executive plans (`scripts/executive_planner.py`) must remain strictly grounded in verified Playbooks and owner-approved Atoms, never raw notes or unapproved candidates.

## Milestone Status

1. Multi-memory typed Refinery — **Complete** (`scripts/ollama_refinery.py`).
2. Operational stats store for retrieval and utility metrics — **Complete** (`brain/.sidecar/telemetry.sqlite3` + durable canonical correction log `brain/0-raw/archive/atom-corrections.jsonl` in `scripts/cognitive_core.py`).
3. Memory fitness reports — **Complete** (`second-brain cognitive fitness`, `GET /v1/cognitive/fitness`, context-profile & fitness-aware ranking in `scripts/recall.py`).
4. Experience and reflection records — **Complete** (`brain/4-experiences/`, `POST /v1/cognitive/experiences`).
5. Procedure verification and skill-candidate tracking — **Complete** (owner-gated Skill Playbooks in `brain/5-playbooks/`, `scripts/skill_playbooks.py`, `second-brain playbooks`, `GET /v1/playbooks`).
6. Executive planner using mature skills and memory, not raw notes — **Complete** (`scripts/executive_planner.py`, `second-brain plan`, `POST /v1/plan`).
7. Stage 4 Executable Skills & Sandboxed Runtime — **Complete** (`brain/6-skills/`, `scripts/executable_skills.py`, `second-brain skills`, `/v1/skills/*`, verified by `scripts/stage4_executable_skills_tests.py`).
