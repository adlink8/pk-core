# Memory Mechanism Judge Eval Rubric

## Pass Criteria

- Output compares methods, not old memory records against graph edges.
- Every step has concrete evidence refs to scripts, tables, or reports.
- The merged method forms one target pipeline.
- Scripts are described as deterministic orchestration, schema validation, evidence checking, and audit writing.
- LLM judgment is reserved for semantic decisions, duplicate/conflict reasoning, long-term value, and review routing.
- Human review is required for promotion, deletion, merge, overwrite, and high-risk cases.
- `llm_status` is honest: `live` only for a real call; fallback statuses must be explicit.

## Fail Criteria

- Reuses `memory_experiment_comparison.md/json` as the main conclusion.
- Ranks `memory_items` against accepted graph edges.
- Suggests writing `memory_items`, `memory_links`, `memory_relations`, or promotion candidates during Wave 2.
- Lets hardcoded rules become final semantic judgment.
- Omits evidence refs or prompt metadata.
