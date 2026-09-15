# Retrieval infrastructure

## Responsibility

Vector build/search adapters and layered retrieval composition.

## Boundaries

Index implementations sit behind contracts; no ownership of knowledge semantics.
Collection/retrieval *evaluation* scripts live under `evaluation/vector/`.
Files here named `evaluate_vector_*` / `compare_*_generations` are re-export
shims only — prefer `personal_knowledge.evaluation.vector.*` in new code.

## Entry points

Use vector builders and unified search contracts in this package.

```powershell
python -m personal_knowledge.evaluation.vector.evaluate_vector_collections
rag-search stats --json
```

## I/O and privacy

Embeddings/indexes are R3 private generated artifacts with generation lineage.

## Tests

Vector, retrieval, fallback and evidence contract tests under `tests/`.

## Ownership

Owner: retrieval. Status: supported.
Last layout review: 2026-07-16 (Phase 22; eval ownership in evaluation/).
