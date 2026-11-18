# Spec: Unified Local AI Brain

> **Document status:** Architecture proposal, not the current deployment
> contract. MCP is an opt-in local stdio interface; do not register it with
> cloud-connected clients. See [Dashboard & Local Interface Security](dashboard-security.md).

## Objective

Build Second Brain into one private, offline-capable local intelligence system.
The local AI runtime is the active reasoning engine. The Markdown vault is the
durable long-term memory body. Embeddings, ChromaDB, JSON vector fallback,
dashboards, MCP tools, and scripts are recall and tool-use layers around that
body, not separate products.

Success means a user can capture messy input, preserve the raw source, refine
it into auditable typed memories, search and reason over those memories, review
AI-generated changes, and use the same memory layer while planning, coding,
researching, deciding, and creating.

## Assumptions

- The system remains local-first and Windows-friendly.
- Ollama is the default local model runtime.
- Markdown files under `brain/` remain the source of truth.
- `brain/.chroma/` and any JSON vector fallback are rebuildable recall layers.
- Raw captures and scraped sources must remain auditable after ingestion.
- AI-generated memories start as tentative until reviewed.
- Creative image and video generation are optional modules activated only when
  local tools/models are installed.

## Tech Stack

- Python scripts with inline `uv` metadata.
- Ollama for local generation, embeddings, and vision when available.
- ChromaDB for persistent vector search, with JSON fallback.
- Streamlit dashboard for local UI.
- Opt-in, limited local MCP stdio interface; no cloud-host registration.
- Markdown plus YAML frontmatter for durable memory.

## Commands

- Smoke test: `uv run scripts/smoke_test.py`
- Health check: `uv run scripts/doctor.py`
- Terminal chat: `uv run chat_with_memory.py`
- Refinery batch: `uv run scripts/ollama_refinery.py`
- Single-source refinery canary: `uv run scripts/ollama_refinery.py --file brain/0-raw/example.md --keep-source --skip-critic`
- Editor shift: `uv run scripts/ollama_editor.py`
- Web scout: `uv run scripts/ollama_scout.py`
- Dashboard: `.\run_dashboard.ps1`
- Watchdog daemon: `.\run_daemon.ps1`
- Full night shift: `.\run_night_shift.ps1`

## Project Structure

- `brain/0-raw/`: raw captures and intake pointers.
- `brain/0-raw/archive/`: processed raw captures, retained for audit.
- `brain/sources/`: cleaned source documents from web or file ingestion.
- `brain/1-desk/`: scratch workspace for intermediate processing.
- `brain/2-atoms/`: typed atomic memories, one claim/event/procedure/system fact per file.
- `brain/3-threads/`: synthesis documents linking related atoms.
- `brain/5-playbooks/`: owner-gated CAS Skill Playbooks and `.history/` revisions.
- `docs/runbooks/`: human-readable operator workflows and Markdown templates (`docs/runbooks/templates/`).
- `brain/.chroma/`: rebuildable vector recall layer.
- `scripts/`: local engine core.
- `dashboard/`: local operator interface.
- `docs/`: architecture and design notes.
- `tasks/`: implementation plan and task queue.

## Memory Contract

Every durable memory must be human-readable Markdown with YAML frontmatter.
The minimum atom schema is:

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
review_status: pending
```

Allowed `memory_type` values:

- `semantic`: facts, claims, concepts, principles, and reference knowledge.
- `episodic`: events, observations, decisions, interactions, and outcomes.
- `procedural`: workflows, playbooks, checklists, and repeatable methods.
- `self`: system capabilities, configuration, constraints, and tool knowledge.

Allowed `review_status` values:

- `pending`: generated or imported, not yet accepted.
- `accepted`: reviewed and trusted enough for normal retrieval.
- `needs_edit`: useful but requires human or AI-assisted correction.
- `rejected`: retained for audit but excluded from normal recall.
- `merged`: superseded by another memory after duplicate or synthesis review.

## System Boundaries

Always:

- Preserve raw sources before or during ingestion.
- Treat Markdown as source of truth.
- Make recall indexes rebuildable.
- Prefer small canary runs before broad automation.
- Run `uv run scripts/smoke_test.py` after changes to core formatting, paths, or ingestion.
- Make AI-generated changes inspectable before trust is upgraded.

Ask first:

- Deleting memories or raw sources.
- Changing the memory schema in a breaking way.
- Adding heavyweight dependencies or online services.
- Enabling autonomous background ingestion beyond existing local folders.
- Adding external APIs to default workflows.

Never:

- Silently overwrite trusted memories.
- Use embeddings as the only copy of a memory.
- Treat unreviewed AI output as established fact.
- Auto-resolve contested claims without a review record.
- Require internet for normal capture, search, review, or chat after setup.

## Target Capabilities

Near term:

- Reliable raw ingestion.
- Clean typed memory atoms.
- Safe source archiving.
- Canary and smoke tests.
- Local model processing.
- Basic ask/search behavior.

Mid term:

- Small batch ingestion.
- Review queue.
- Semantic search with review filtering.
- Duplicate detection.
- Task extraction.
- Stronger source linking.
- Project-aware querying.

Long term:

- A full local cognitive workspace that remembers, reasons, builds, creates,
  reviews itself, and supports project continuity over months or years.

## Success Criteria

- A raw capture can become one or more typed atoms without losing the source.
- Every generated atom has source, confidence, importance, context, lifecycle,
  and review metadata.
- Search can retrieve accepted memories and cite filenames.
- The review workflow can accept, edit, reject, merge, or supersede memories.
- The vector store can be deleted and rebuilt from Markdown.
- Project work can write durable decisions, tasks, and architecture memories.
- The system remains usable offline once local models and packages are installed.

## Open Questions

- Should unreviewed `pending` memories be included in chat by default, or only
  shown as lower-trust context?
- Should review records live inline in each atom, in a separate log, or both?
- Which local coding agent interface should be the first project-aware target:
  dashboard, MCP, CLI chat, or all three behind one shared query layer?
- Which local image-generation stack should be supported first if installed?
