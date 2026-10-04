# Audit and usage metering

Memoratum records operational accountability locally in SQLite. These records
are not telemetry: they never leave the process and no network client is used.

## Audit events

Mutating and security-sensitive operations append an event containing:

- a local epoch timestamp and opaque event id;
- the actor kind (`admin`, `key`, or `anonymous`) and a SHA-256 key fingerprint;
- the effective `containerTag` and `org_id` scope;
- an action name, resource type/id, outcome, and small JSON metadata.

Raw API keys, document content, fact text, request bodies, and IP addresses are
never stored. Audit reads are administrator-only and paginated.

## Usage counters

Each authenticated key has cumulative counters for requests, searches, document
writes, fact writes, and input characters. Wildcard and legacy-null scopes are
represented explicitly in the counter key. Counters are local billing seams,
not an external analytics stream.

The initial API surface is:

- `GET /v4/audit?containerTag=&org_id=&limit=&offset=` — administrator-only
  audit events;
- `GET /v4/usage?containerTag=&org_id=` — administrator-only counters.

Both endpoints use the existing error envelope and never expose raw credentials.

## Metrics endpoint

`GET /metrics` serves Prometheus text exposition format (`text/plain;
version=0.0.4`) and is loopback-bound like `/health`.

This is **not telemetry**. It is an inbound pull server: the server opens no
outbound connection, performs no DNS lookup, and transmits nothing. The project
already ruled on this for audit records — they "never leave the process and no
network client is used" — and a local exposition endpoint is the same kind of
thing. `scripts/check_no_telemetry.py` matches vendor SDK names and does not
match a local route.

Two deliberate limits on what it exposes:

* **Labels never include `container_tag`, `org_id` or `project_id`.** They are
  unbounded by construction and would multiply by histogram bucket count.
  Per-tenant accounting belongs to `/v4/usage`, which is a different question.
* **Route labels are path templates, never concrete paths.** A concrete path
  embeds memory and document ids, which would both explode cardinality and leak
  identifiers into a metrics store.

The endpoint carries counts and latencies only — no content, no secrets, no
error strings.

Metrics are evidence about the process, not about usage. For usage and audit
accountability see the rest of this document.
