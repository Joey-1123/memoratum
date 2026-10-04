# Memoratum Constitution

## Core Principles

### I. Self-Hosted and Local-First by Default

Memoratum MUST run to completion on a single machine with no external service.
SQLite MUST remain the default and fully sufficient store. Every integration with
a hosted provider — LLM, embeddings, and vector backends — MUST be an optional
extra that degrades to a documented local behaviour when absent, never a hard
requirement. The server MUST NOT emit external telemetry or analytics;
`scripts/check_no_telemetry.py` enforces this over `src/`, `clients/`, and
`dashboard/src/` and MUST run in CI. Network egress is permitted only to
providers the operator explicitly configures and to webhooks the operator
explicitly opts into.

Rationale: the project's value is that a team can run its agents' memory
infrastructure without a vendor, a bill, or a phone-home. Any change that makes
an external service load-bearing trades away the reason the project exists.

### II. Hard Isolation Between Scopes

Every read and write MUST be constrained by container tag and, where applicable,
organization and project. A caller MUST NOT read, write, or infer the existence of
a record outside its resolved scope. Authorization failures that would confirm a
resource exists MUST return a uniform 404 rather than 403, so the API cannot be
used to probe for identifiers. Keys bound only to a container tag MUST NOT be
usable as control-plane credentials. An external identity MUST have a real
membership row before it carries any scope. Newly minted project keys MUST
default to the least privileged role, and data mutations plus project and webhook
management MUST require project-bound ownership.

Rationale: this is a multi-tenant store holding other people's context. Leaking or
even confirming a record across a tenant boundary is a security incident, not a
bug, and the uniform-404 rule exists because a distinguishable error is itself an
oracle.

### III. Durable and Recoverable Work

A mutation that requires follow-up work MUST enqueue that work in the same
transaction as the state change it depends on. A single logical operation MUST have
a single commit boundary; helper functions MUST NOT commit on their caller's
behalf. Work claimed by a process MUST carry a lease with a heartbeat and a
reaper, so that the death of any worker — including an uncatchable kill — leaves
work recoverable rather than permanently stranded. Any job that is not actively
progressing MUST be cancellable by an operator. A failure in background work MUST
NOT terminate the worker process.

Rationale: an unrecoverable job silently destroys a tenant, and the only symptom is
a status code that never changes. This principle exists because a hardened
invariant — never steal a running job — was implemented without its counterpart —
recover a dead one — and the test suite encoded only the half that was safe.

### IV. Evidence Before Assertion

A change is not verified until a runnable check demonstrates it, and the check
MUST be executed rather than assumed. Every bug fix MUST ship with a regression
test that fails when the fix is reverted. Tests MUST exercise real code paths;
mocks and monkeypatching MUST NOT stand in for the behaviour actually being
asserted. Claims about performance, concurrency, and throughput MUST be backed by a
measurement committed to the repository, not by reasoning about the code. A green
suite is necessary but MUST NOT be reported as evidence of production readiness.

Rationale: a full suite of passing tests coexisted with an unrecoverable outage, a
bypassable SSRF guard, and search latency that grew linearly to over a second.
None of those were visible to the tests, because no test injected a failure.

### V. Provider-Neutral, Minimal Dependencies

Core runtime dependencies MUST stay minimal, and a new runtime dependency MUST be
justified against the Python standard library before it is accepted. Every
provider integration MUST live behind an optional extra and MUST be reachable
through one interface, so that swapping providers is configuration and never a
code change. The licence split is binding: server code is AGPL-3.0-or-later and
client and SDK code is MIT, and no change may blur that boundary.

Rationale: a self-hosted project that accumulates mandatory dependencies becomes
one it cannot run offline, and a dependency added for convenience is a permanent
maintenance and supply-chain obligation.

## Security and Operational Constraints

- Secrets MUST NOT be stored in plaintext. Webhook signing secrets are encrypted at
  rest, and key material MUST be supplied by the operator or generated into the
  data directory with restrictive permissions.
- Outbound requests to operator-configured URLs MUST defend against server-side
  request forgery. Validation alone is insufficient: a resolved address MUST be
  pinned for the connection that follows, so that a hostname which changes its
  answer between validation and connection cannot reach an internal or
  link-local destination. Redirects MUST NOT be followed to an unvalidated host.
- Network calls MUST be bounded by an explicit timeout and a response size limit.
- Audit and usage records MUST be retained locally and MUST NOT be transmitted.
- Backup MUST use SQLite's online backup mechanism so that write-ahead log content
  is captured, and restore MUST verify integrity before and after writing.
- Destructive operations MUST be recoverable or explicitly irreversible in the
  documented API contract.

## Development Workflow and Quality Gates

The following MUST pass before a change is proposed for merge, and the same
commands MUST be run locally first:

- `uv run python scripts/check_no_telemetry.py`
- `MEM0_TELEMETRY=false uv run pytest tests/test_mem0_official_sdk.py
  tests/test_mem0_official_lifecycle.py tests/test_mem0_webhooks.py -q --no-cov`
  (`--no-cov` because the coverage floor below is a property of the full suite;
  a narrow subset cannot satisfy it)
- `uv run ruff check .` and `uv run ruff format --check .`
- `uv run pytest -q`
- `npm test --prefix clients/ts`
- `node --check clients/opencode/memoratum.js`
- `npm ci --prefix dashboard` and `npm run build --prefix dashboard`

Dependency risk MUST be checked on change: `pip-audit --local` for the Python
environment and `npm audit --audit-level=high` for each JavaScript workspace.
Security scanning runs as its own workflow and is not optional.

Changes land on a branch, are proposed as a pull request to `main`, and merge with
a merge commit. Squash and rebase merges are not used in this repository. A
pull request MUST state what was verified and how, and MUST NOT describe a fix as
tested unless the corresponding test was executed.

Documentation under `docs/` MUST be updated in the same change as the behaviour it
describes. A behaviour that is documented but not implemented is a defect, as is
an implemented behaviour that contradicts the documentation.

## Governance

This constitution supersedes informal conventions, individual preference, and
agent-generated defaults. Where a change conflicts with it, the constitution wins
and the change MUST be amended first.

Amendments require a written rationale, a version bump, and a pull request that
records what changed and why. Versioning follows semantic versioning applied to
governance: MAJOR for removing or redefining a principle, MINOR for adding a
principle or materially expanding guidance, PATCH for clarifications and
corrections that do not change obligations.

Every pull request MUST be checked for compliance before merge, and a reviewer
who finds a conflict MUST block the merge rather than waive the principle in
conversation. Obligations that the code does not yet satisfy are recorded as
debt with an owner, not quietly dropped from this document.

Runtime development guidance is distributed across `docs/ARCHITECTURE.md`,
`docs/SECURITY.md`, and `docs/OPERATIONS.md`; those documents MUST NOT contradict
this constitution.

**Version**: 1.0.0 | **Ratified**: 2026-10-03 | **Last Amended**: 2026-10-03