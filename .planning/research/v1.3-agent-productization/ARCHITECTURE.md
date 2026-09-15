# v1.3 Architecture Research

```text
ChatGPT
  → HTTPS tunnel → Node HTTP MCP /mcp
  → shared Python read/orchestration services
  → immutable External / Analysis / Pilot / Calibration authorities
```

- One user intent per tool; read tools are idempotent and accurately annotated read-only.
- Mutating tools use preview-checksum-bound, short-lived confirmation plus expected sequence and idempotency key.
- Return compact `structuredContent`; fetch large evidence only through explicit drill-down and keep non-model UI payload in `_meta` when applicable.
- The model may choose and narrate tools, but deterministic services own lifecycle, privacy, conflict, freshness, risk and confirmation gates.
- Existing CLI, REST and MCP transports share services so behavior cannot drift by interface.
