# Compatibility entrypoints

## Responsibility
Temporary v1.1 command shims that forward to `personal_knowledge` modules.

## Boundaries
No production implementation or private data. New consumers are prohibited.

## Entry points
Use the five `rag-*` console commands; these files exist only for rollback compatibility.

## I/O and privacy
Identical to the canonical target; the shim stores no data.

## Tests
`python integration/scripts/governance/check_shim_budget.py --check`

## Ownership
Owner: platform. Status: compatibility. Remove only after the governed retirement gate.
