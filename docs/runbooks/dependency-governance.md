# Dependency governance

## Supported runtimes

- Python 3.12 and 3.14 are tested in CI.
- Node.js 20 is tested for `apps/personal_data_chatgpt`.
- The default test suite is offline and does not require private fixtures.

## Python dependency surfaces

- `requirements.txt`: core runtime and governance dependencies.
- `requirements-dev.txt`: deterministic test tooling; includes core.
- `requirements-optional.txt`: UI, hosted-model and local-embedding stacks.
- `constraints.txt`: reviewed exact versions for every declared direct dependency.

Use a fresh virtual environment; never install into the machine-wide interpreter:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -c constraints.txt -r requirements-dev.txt
.venv\Scripts\python -m pytest --collect-only -q
.venv\Scripts\python -m pytest -q
```

Install an optional stack only when its feature is needed. Local embeddings are
large and are intentionally not part of the core/CI environment.

## Node dependency surface

The Apps adapter currently has no third-party packages. Its lockfile is still
tracked so manifest/lock drift is detected and future packages use `npm ci`:

```powershell
npm ci --ignore-scripts --prefix apps/personal_data_chatgpt
npm test --prefix apps/personal_data_chatgpt
```

## Continuous gates

Run the same governance entrypoint used by CI:

```powershell
python -m personal_knowledge.governance.preflight --ci
# Optional HTML render (paths after Phase 20):
python integration/scripts/governance/render_governance_report.py `
  --preflight var/runtime/governance/preflight.json `
  --history var/runtime/governance/governance_history.sqlite `
  --output var/runtime/governance/governance_report.html
```

History and HTML stay under ignored runtime storage (`var/runtime/`, Phase 20)
and contain aggregate gate outcomes only. P0 findings, including detected
credentials, cannot be grandfathered. Existing non-P0 debt is allowed only by an
exact finding ID in the reviewed baseline.

