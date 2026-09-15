# v1.3 Stack Research

- Primary ChatGPT App archetype: **tool-only**. Phase 32–33 do not require a widget.
- Reuse the existing Python service layer, stdio MCP server, Node HTTP MCP bridge, REST API, SQLite authorities and PowerShell watchdog.
- Keep `/mcp` as the ChatGPT connector endpoint and use the existing HTTPS tunnel for local live acceptance.
- Do not add an OpenAI API-key dependency: provider calls remain behind the existing authenticated/local execution path.
- Add no production dependency unless an existing MCP/Apps SDK contract cannot be met with the current stack.

Sources: OpenAI Apps SDK `Build your MCP server`, `Define tools`, `Connect from ChatGPT`, and `Reference` (checked 2026-07-18).
