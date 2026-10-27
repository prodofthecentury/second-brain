# External Architecture Review — Pass 1

You are the external architectural critic for a local-first AI second-brain project. The user wants the most capable, fast, helpful, privacy-preserving offline system realistically achievable on one machine. They explicitly want rigorous back-and-forth, not a generic RAG outline.

You may inspect the current repository using **only** Read, Glob, and Grep. Do not write, execute commands, use network tools, install anything, or change files.

## Relevant current state

- Core promise: durable Markdown memory source of truth; indexes are rebuildable; local Ollama generation/embedding; offline operation for core memory workflows; optional explicit web scout.
- Existing components: multimodal ingestion, raw archive/manifest, atom and thread notes, ChromaDB with JSON fallback, Graph-RAG-style recall, Streamlit dashboard, CLI chat, filesystem daemon, and MCP server.
- Current sweep: syntax compile and smoke tests pass. The machine currently has no reachable Ollama service, `llama3.2:3b` and `nomic-embed-text` are absent, no vision fallback is found, ChromaDB persistent client is unavailable, dashboard is down, 23 raw captures and 14 URLs are queued, and this directory is not a Git repository.
- The existing `docs/cognitive-core.md` rejects autonomous self-modifying code, uncontrolled continual fine-tuning, autonomous deletion, and an executive planner before typed/reviewed memory is reliable.

## Pass-1 task

Read at least: `README.md`, `docs/unified-local-ai-brain.md`, `docs/cognitive-core.md`, `tasks/plan.md`, `tasks/todo.md`, and the core scripts that define formatting, storage, retrieval, review, and MCP boundaries. Then provide a detailed, concrete critique and a proposed target architecture.

Evaluate every layer: user experience; model/runtime routing; memory taxonomy and lifecycle; provenance; write approvals; retrieval/reranking/context assembly; graph semantics; local search/index choices; ingestion and multimodal pipeline; agent/tool boundary; task/execution memory; scheduling; observability; data integrity/backups; security/privacy; performance/resource governance; offline behavior; migration; and test strategy.

Do not give implementation code or a generic technology list. Be adversarial. Identify hidden assumptions, failure modes, unsafe or misleading claims in the current system, priority trade-offs, and the smallest architecture that can later become exceptionally capable. Distinguish conclusions supported by repository evidence from hypotheses that need measurement. Finish with 8–12 precise questions or decisions that the primary architect should challenge in a second pass.
