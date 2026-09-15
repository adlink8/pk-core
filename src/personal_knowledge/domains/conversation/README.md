# Conversation domain

## Responsibility

**Compat package only.** Rules historically lived here; build/eval orchestration
moved in Phase 21. Remaining modules are re-export facades to
`application.conversation` / `evaluation.conversation`.

## Boundaries

- No retrieval ranking, UI, or direct production promotion.
- No LLM client primitives (those are in `core/llm.py`).
- Do not add new logic under `domains/conversation/`.

## Canonical locations

| Kind | Package |
|------|---------|
| Build / summary / graph / vector-store | `personal_knowledge.application.conversation` |
| Product sync CLI | `personal_knowledge.application.sync` (`pk-sync`) |
| Eval / compare / eval-set | `personal_knowledge.evaluation.conversation` |
| LLM client + retry | `personal_knowledge.core.llm` |

## Entry points

Compatibility imports only; product callers use `pk-sync` or canonical
`application.conversation` modules.

## I/O and privacy

Facades add no I/O. Canonical conversation code enforces local-only private data
and read-only AgentsView access.

## Tests

Conversation normalization and contract tests under `tests/`.

## Ownership

Owner: conversation. Status: supported compatibility re-export only.
Last layout review: 2026-07-16 (Phase 22; facade debt clear).
