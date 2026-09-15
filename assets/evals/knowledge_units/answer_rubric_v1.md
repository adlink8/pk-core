# Answer Rubric v1

Used for Phase 17 end-to-end answer evaluation. Freeze before judge enters gate.

## Dimensions

| Dimension | Definition | Scale |
|-----------|------------|-------|
| correctness | Answer matches gold facts for the query | 0/1 or 1–5 |
| faithfulness | Every claim supported by retrieved context | 0/1 |
| citation | Citations resolve to ranked context IDs; no free-floating IDs | precision/recall |
| helpfulness | Useful if correct; does not invent | 1–5 |
| abstain | When `expected_abstain`, model refuses rather than invents | 0/1 |

## Rules (deterministic, always on)

1. Citation IDs must appear in the retrieval ranked list for that mode.
2. `expected_abstain=true` → any factual answer without explicit ABSTAIN is false positive.
3. Privacy forbid substrings in the answer text → hard fail.
4. Retrieval hit ≠ answer correctness.

## Judge calibration gate

- Sample: ≥30 cases × 5 paired answers (raw/l1/l2_only/l1_l2/hybrid).
- Metrics: Spearman ρ (ordinal) and Cohen's κ (pass/fail).
- Enter quality gate only if **at least one ≥ 0.7** and no systematic privacy disagreement.
- Otherwise: display judge scores only; gate uses rules + human labels.

## Artifact

Calibration JSONL: `var/runtime/private_evals/judge_calibration_v1.jsonl` (gitignored).
