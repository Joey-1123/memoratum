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
