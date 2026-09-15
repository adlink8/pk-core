## Conflict Detection Report

### BLOCKERS (0)

None.

### WARNINGS (0)

None.

### INFO (4)

[INFO] Legacy and canonical planning roots coexist
  Found: Phase artifacts were under `.gsd/phases/` while state and codebase maps were under `.planning/`.
  Note: Files were copied into `.planning/phases/`; `.gsd/` remains an immutable migration source.

[INFO] Stale status prose exists in legacy documents
  Found: Some CONTEXT/PLAN/STATE sections still say Planned, Partial, or 待执行 after later verification artifacts report completion.
  Note: Migration status precedence is VERIFICATION/SUMMARY, then PLAN frontmatter, then legacy CONTEXT prose.

[INFO] Phase 08 remains deferred
  Found: Legacy STATE marks Phase 08 active while later work proceeded through Phase 14.
  Note: Phase 08 is preserved as deferred work and does not block explicit Phase 14 planning.

[INFO] Automated subagents disabled during migration
  Found: Standard GSD agent profiles are not installed and the user disallows GPT-5.6 Sol subagents.
  Note: `.planning/config.json` keeps parallelization disabled until explicit 5.5/5.4 profiles are configured.
