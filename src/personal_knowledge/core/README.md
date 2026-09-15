# Core
## Responsibility
Foundation contracts, paths, IDs and narrow shared utilities.
Includes **`llm.py`**: a small OpenAI-compatible facade backed by one
capability-protected Pi Kernel task per completion. The old OpenAI-compatible
client is retained only behind explicit `PI_KERNEL_LEGACY_MODE=1` rollback.
## Boundaries
Cannot import domain, pipeline, service or evaluation modules.
## Entry points
Import from `personal_knowledge.core` (e.g. `project_paths`, `runtime_config`,
`llm.make_llm_client` / `llm._chat_with_retry`).
Legacy `integration.scripts.core` shims may re-export during compatibility.
## I/O and privacy
No direct raw/private content reads. Path resolution prefers Phase 20 trees
(`data/`, `var/`, `archive/`) via `project_paths`, with optional legacy fallback.
AgentsView live DB stays external and read-only. Raw prompts/responses used by
the compatibility facade remain in process memory; Pi stores receive only
task/session/event metadata and checksums.
`privacy_guard` is the shared exit sealer for MCP/REST retrieval payloads
(keys, tokens, PEM, assignment secrets → `[PRIVACY:…]`).
## Tests
Core contracts and path tests under `tests/` and `tests/governance/`.
## Ownership
Owner: platform. Status: supported. Last reviewed: 2026-07-16 (Phase 22 docs pass).

