# Services
## Responsibility
CLI/API/MCP/dashboard delivery adapters.
## Boundaries
Calls application/domain contracts; does not implement ranking or data mutation rules.
## Entry points
Supported API, MCP and dashboard modules in this package.
## I/O and privacy
Responses expose minimum necessary fields; private content remains local and access-controlled.
All MCP tool text and REST JSON exits pass `personal_knowledge.core.privacy_guard` (v2):
credentials (cloud API keys, tokens, PEM, URL passwords, assignment/env/zh labels),
PII (email, CN phone/id, Luhn bank cards), and sensitive JSON field values are sealed as
`[PRIVACY:<kind>:fp:<hmac12>]` (default) or Fernet when
`PERSONAL_DATA_PRIVACY_MODE=encrypt` + cryptography.
Scope: `PERSONAL_DATA_PRIVACY_SCOPE=credentials,pii,fields` (default all).
Disable only for local debug: `PERSONAL_DATA_PRIVACY_GUARD=0`.
## Tests
CLI, REST, MCP and dashboard contract tests under `tests/`.
## Ownership
Owner: delivery. Status: supported.

