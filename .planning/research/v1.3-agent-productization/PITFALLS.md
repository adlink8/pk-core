# v1.3 Pitfalls

- Over-broad tools make ChatGPT routing unreliable; keep list/get/explain/prepare/confirm operations separate.
- Incorrect `readOnlyHint`/`destructiveHint`/`openWorldHint` metadata can create unsafe approval behavior.
- Returning entire evidence JSON wastes model context and may leak private fields; default to bounded summaries.
- A confirmation not bound to exact preview checksum permits time-of-check/time-of-use drift.
- ChatGPT retries can duplicate provider calls or append-only records unless idempotency is enforced end-to-end.
- Starting a tunnel without checking `/mcp` readiness produces a connector that appears online but cannot execute.
- A successful demo must not be described as comparative effectiveness; Phase 31 remains `INCONCLUSIVE` until a sufficient cohort exists.
