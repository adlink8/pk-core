# Evaluation

## Responsibility

Frozen suites, ablations, metrics, reports, and promotion gates.
**Canonical owner** of subdomain and vector eval scripts (Phase 21).

## Layout

```
evaluation/
  conversation/   # cutover, prompt/quality eval, compare_summaries, eval-set
  graph/          # relation judgment eval
  knowledge/      # canary, extraction, RAG eval
  memory/         # depth / promotion / relation / experiment suites
  vector/         # evaluate_vector_*, compare_*_generations (from retrieval/)
  run_knowledge_eval.py, gate_knowledge_candidate.py, …
```

## Boundaries

Reads public contracts/candidates; cannot silently mutate active generations.
Promotion remains an **application** / `pk-ku promote` decision after eval PASS.

## Entry points

```powershell
python -m personal_knowledge.evaluation.run_knowledge_eval --help
python -m personal_knowledge.evaluation.vector.evaluate_vector_collections
python -m personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary

# Product path for canary labels / strict gate
pk-ku canary --help
```

Legacy imports via `domains.*` / `retrieval.evaluate_*` re-exports still work for
compat callers; **new code must import `evaluation.*`**.

## I/O and privacy

Evaluation consumes frozen local datasets and candidate artifacts. Reports must
store metrics, stable IDs, and redacted evidence only; private message bodies and
credentials must not leave the local governed workspace. Evaluation never
promotes an index by itself.

## Tests

```powershell
python -m pytest -q tests/test_knowledge_eval_*.py
python -m pytest -q tests/unit/test_vector_collection_eval.py
```

## Ownership

Owner: evaluation. Status: supported.
Last layout review: 2026-07-16 (Phase 22; facade debt clear).
