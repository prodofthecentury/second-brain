# Implementation Plan: Unified Local AI Brain

## Current checkpoint — 2026-10-24

The trust and safety implementation slice is in place and its focused suite and
project smoke suite pass. The current machine doctor reports a current lexical
index, zero currently approved memories, no verified dense generation, a
reachable Ollama service with configured text/embed models, an unavailable
vision model, and a stopped dashboard. Do not treat the system as a trusted
release: retrieval evaluation, adversarial failure injection, DNS-rebinding
hardening, and the final Git migration commit remain open. See
`docs/system-sweep-2026-10-24.md`.

## Overview

The project already contains the main infrastructure pieces: Markdown vault,
typed atom formatting, Ollama refinery, vector search, dashboard, MCP server,
web scout, and smoke tests. The next work is to make these pieces behave as one
system by strengthening the memory lifecycle, review workflow, shared recall
contract, and project-aware use cases.

## Architecture Decisions

- Markdown remains the durable memory source of truth.
- Raw captures and scraped sources stay archived for audit.
- Vector stores are rebuildable indexes, not authoritative storage.
- AI-generated atoms default to `review_status: pending`.
- Review and duplicate handling must be explicit before automation expands.
- Shared memory/search behavior should move into reusable script modules so CLI,
  dashboard, MCP, and future coding workflows do not drift.

## Phase 1: Foundation

- [ ] Task 1: Add review lifecycle fields to memory formatting.
- [ ] Task 2: Add source archive manifest records.
- [ ] Task 3: Add vector rebuild command from Markdown atoms.

### Checkpoint: Foundation

- [ ] `uv run scripts/smoke_test.py` passes.
- [ ] A canary raw file can be processed without losing its source.
- [ ] Deleting `brain/.chroma/` does not lose memory content.

## Phase 2: Reviewable Ingestion

- [ ] Task 4: Create a review queue view over pending atoms.
- [ ] Task 5: Add accept, reject, needs-edit, and merge transitions.
- [ ] Task 6: Exclude rejected memories from normal search.

### Checkpoint: Review Loop

- [ ] Pending memories are visible and editable.
- [ ] Accepted memories are retrieved normally.
- [ ] Rejected memories remain auditable but are not injected into chat.

## Phase 3: Better Recall

- [x] Task 7: Centralize search and Graph RAG into a shared module.
- [ ] Task 8: Add duplicate candidate detection.
- [x] Task 9a: Add metadata-aware ranking hooks for memory type, confidence,
  importance, review status, and contexts.
- [ ] Task 9b: Add freshness-aware ranking and operational utility metrics.

### Checkpoint: Recall

- [x] CLI chat, dashboard, and MCP use the same search contract.
- [x] Search results include filenames, review status, and confidence.
- [ ] Duplicate candidates are suggested, not auto-merged.

## Phase 4: Project-Aware Intelligence

- [ ] Task 10: Add project memory types for decisions, tasks, goals, and open
  questions using the existing atom schema.
- [ ] Task 11: Add a project-aware query command that scopes retrieval by
  project/context tags.
- [ ] Task 12: Add experience records for completed coding or research sessions.

### Checkpoint: Project Partner

- [ ] The system can recall architecture decisions and open tasks by project.
- [ ] A completed work session can produce reviewable memories.
- [ ] Project-aware retrieval works offline.

## Phase 5: Creative Modules

- [ ] Task 13: Add capability detection for local image generation tools.
- [ ] Task 14: Add asset planning memories and generation request records.
- [ ] Task 15: Defer video generation behind explicit capability checks.

### Checkpoint: Creative

- [ ] Creative generation is optional and local.
- [ ] Missing models fail with clear instructions, not broken workflows.
- [ ] Generated asset prompts and decisions can be stored as memories.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| AI-generated memories become trusted too early | High | Default to `pending`; filter by review status |
| Search behavior forks across CLI, dashboard, MCP | High | Centralize recall in one shared module |
| Raw sources are lost after ingestion | High | Archive first and add source manifest |
| Large batch automation corrupts many notes | High | Canary tests and small batches before daemon automation |
| Offline promise is broken by hidden network calls | Medium | Make internet-only modules explicit and optional |
| Git is unavailable or misconfigured | Medium | Keep filesystem-level archives and smoke tests independent of Git |

## Open Questions

- Should pending memories appear in chat as clearly labeled low-trust context?
- Should review actions be implemented first in CLI, dashboard, or both?
- Should source archive manifests be one JSONL file, one Markdown log, or one
  sidecar per archived source?
