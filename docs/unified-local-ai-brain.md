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
- Creative image and video generation are optional modules (`scripts/creative_media.py`, `second-brain media`, `/v1/media/*`) gated by hardware capability profile (`scripts/hardware_profile.py`) and outbound DLP policy controls, with zero cloud dependency (`builtin_procedural_local` Pillow + pure-Python ISO-BMFF `.mp4` / `.gif` synthesis out of the box, plus loopback-only `sd_webui_local`, `comfyui_local`, and offline `diffusers_local` adapters).

## Tech Stack

- Python scripts with inline `uv` metadata.
- Ollama for local generation, embeddings, and vision when available.
- ChromaDB for persistent vector search, with JSON fallback.
- Streamlit dashboard for local UI.
- Opt-in, limited local MCP stdio interface; no cloud-host registration.
- Markdown plus YAML frontmatter for durable memory.

## Commands

- Health & gates: `uv run --locked second-brain doctor`, `uv run --locked second-brain gates status`
- Unified test gate (`34` suites): `uv run --locked python scripts/test_all.py`
- Smoke test: `uv run --locked python scripts/smoke_test.py`
- Terminal chat & capture: `uv run --locked second-brain chat "<question>" --mode stream`, `uv run --locked second-brain capture "<note>"`
- Refinery batch / single-source canary: `uv run --locked python scripts/ollama_refinery.py`, `uv run --locked python scripts/ollama_refinery.py --file brain/0-raw/example.md --keep-source --skip-critic`
- Create-only thread synthesis: `uv run --locked python scripts/ollama_editor.py`
- DNS-pinned Web scout: `uv run --locked python scripts/ollama_scout.py`
- Local creative media: `uv run --locked second-brain media status`
- Dashboard / Daemon / Loopback API: `.\run_dashboard.ps1`, `.\run_daemon.ps1`, `uv run --locked second-brain serve --port 8765`

## Project Structure

- `brain/0-raw/`: captures, quarantined `inbox/`, `note-snapshots/`, and durable `archive/` (processed captures, `decision-record.jsonl`, `vault-ledger.jsonl`, and `snapshots/*.json`).
- `brain/sources/`: captured/extracted source artifacts and `generated/` CAS creative media assets.
- `brain/1-desk/`: temporary working area.
- `brain/2-atoms/`: canonical Markdown atoms and immutable `.history/` revisions.
- `brain/3-threads/`: create-only, non-overwriting synthesis threads.
- `brain/4-experiences/`: create-only Cognitive Core episodic task records.
- `brain/5-playbooks/`: owner-gated CAS Skill Playbooks and `.history/` revisions.
- `brain/6-skills/`: owner-promoted Stage 4 Sandboxed Executable Skills and `.history/` revisions.
- `brain/notes/`: zero-friction working notes (kept separate from verified atoms).
- `docs/runbooks/`: human-readable operator workflows and Markdown templates (`docs/runbooks/templates/`).
- `brain/.sidecar/` & `brain/.chroma/`: strictly disposable rebuildable FTS5/notes/chat/telemetry SQLite DBs, runtime locks, and dense vectors.
- `scripts/`, `dashboard/`, `desktop/`, `extensions/`, `mobile/`: runtime, UIs, IDE/Clipper extensions, and Mobile Drop/Vault Client.
- `docs/` & `tasks/`: architecture specs, benchmark/review artifacts, implementation plan, and task queue.

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

## Resolved Architecture Decisions

1. **Unreviewed Pending Isolation:** Unreviewed `pending` memories are excluded from default verified recall and factual synthesis (requiring `accepted` + current live CAS approval + byte-offset anchor verification) and are surfaced only inside explicit exploratory `Research` view (`[UNVERIFIED LEADS]`) without promotion to verified claims.
2. **Dual Canonical Review State:** Human approval state lives both in human-readable Markdown frontmatter (`review_status`, `claim_state`) and in the append-only canonical decision log (`brain/0-raw/archive/decision-record.jsonl`), enforced against exact current SHA-256 content hashes.
3. **Unified Shared Query Layer:** All surfaces (`dashboard/`, CLI, Electron `desktop/`, VS Code extension, and opt-in 27-tool MCP server) execute over one canonical verification and recall stack (`scripts/recall.py`, `scripts/answer_policy.py`, `scripts/memory_api.py`).
4. **Local Creative Media Stack:** Offline image and video synthesis runs hardware-gated through optional local Diffusers (`local/sdxl-turbo`) and bundled PyAV (`libx264` MP4 / GIF) via `scripts/creative_media.py`.
