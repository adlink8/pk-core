# Decision feedback runbook

Phase 26 keeps facts, observations, inferences, recommendations, human decisions,
action attestations and outcomes as separate immutable record types. A recommendation
does not become a fact, a knowledge unit, a serving member or permission to act.

## Read surfaces

CLI, REST and MCP delegate to one checksum-verifying `DecisionFeedbackService`.
Every read replays the exact `recommendation_published` genesis, all typed rows and
the complete previous-event checksum chain. Responses are metadata-only and omit
private bodies, target prose, unrestricted notes, credentials and executable data.

REST exposes only GET endpoints under `/decision/`. MCP exposes only the five
`decision_recommendation*` read tools. Neither transport can confirm, accept,
reject, record an action/outcome, execute, send, schedule, purchase, publish or
dispatch anything.

## Explicit local writes

Only the local CLI can append a human-attested record. Every write requires all of:

- `--write`
- `--i-confirm <recommendation-id>` matching the exact record
- `--actor-class user` and a 64-character human identity hash
- caller-owned `--expected-sequence` and `--idempotency-key`

Example confirmation:

```powershell
python -m personal_knowledge.intelligence.decision.cli --db <sandbox.sqlite> confirm `
  --recommendation-id <id> --recommendation-checksum <checksum> `
  --decision accept --reason-code user_selected `
  --actor-class user --actor-identity-hash <sha256> `
  --expected-sequence 1 --idempotency-key <caller-key> `
  --occurred-at <ISO-8601> --write --i-confirm <id> --json
```

Acceptance means only that the user selected the proposal. Action records are
non-executable attestations; outcome records are observations. Effectiveness is
an observational inference with `causal_claim=false`, not proof of causal impact.

## Release boundary

The metadata-only acceptance command may exercise a full loop only in an isolated
temporary database, then inspect live metadata read-only. It never applies a schema,
publishes a recommendation, changes lifecycle/serving/pointers/watermarks, calls a
network or paid provider, or performs an external action. Phase 24 human Gold/Judge,
lifecycle review and final quality gates remain independently release-blocking.
