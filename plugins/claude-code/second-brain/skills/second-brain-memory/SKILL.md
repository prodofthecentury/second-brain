---
name: second-brain-memory
description: Search, recall, and store persistent local Second Brain notes and verified atoms across sessions.
---

# Second Brain Memory

Use the `second-brain` MCP tools whenever the user asks about prior decisions, architecture, personal notes, or wants to remember something across sessions:

- `search_memory`: Query verified atoms (`brain/2-atoms/`).
- `search_notes`: Query zero-friction owner notes (`brain/notes/`).
- `get_atom` / `get_note`: Fetch full content and provenance for a specific result.
- `remember`: Capture clean facts or snippets into searchable notes (risky or untrusted content is automatically quarantined to `brain/0-raw/inbox/`).
