# Memory Promotion Judge Eval Rubric

## Deterministic Gates

Reject:

- empty or unparsable `evidence_refs_json`
- empty or unparsable `source_refs_json`
- unexplainable `relation_type`
- missing or empty `proposed_claim`
- claim is a one-time task, transient task chain, error-specific fix, or homework item
- confidence below `0.55`

Require human review:

- no live API/LLM key is available
- upstream status is `needs_live_llm_review`
- upstream status is `reject_or_review`
- duplicate or conflict target is present
- confidence is between `0.55` and `0.75`
- risk flags mention weak evidence, merge, replace, conflict, deletion, or overwrite

Approve:

- all deterministic gates pass
- live LLM judgment is available
- upstream status is not conservative/reject-or-review
- confidence is at least `0.75`
- `human_review_required=false`

## Wave 4 Constraint

Wave 4 is a review gate and controlled-promotion dry run. A zero-approved result
is valid and expected when candidates came from fallback/no-key or upstream
review states.
