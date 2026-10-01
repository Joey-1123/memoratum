# Webhook runbook

Memoratum webhooks are **opt-in**, project-scoped, and delivered from a local
SQLite outbox. They are not telemetry and are disabled until an administrator
creates an endpoint.

## Create a webhook

Use the official Mem0-compatible route shape:

```http
POST /api/v1/webhooks/projects/{project_id}/
Authorization: Token $MEMORATUM_API_KEY
Content-Type: application/json

{"url":"https://example.test/memoratum","name":"Memory Logger","event_types":["memory_add","memory_update"]}
```

The response includes `secret` exactly once. Store it in the receiver's
secret manager; subsequent list/get responses never include it. Secrets are
encrypted at rest with Fernet. Set `MEMORATUM_WEBHOOK_ENCRYPTION_KEY` to a
comma-separated Fernet key list for managed key rotation; otherwise Memoratum
creates a mode-600 `.webhook-encryption-key` in `MEMORATUM_DATA_DIR`. Pause or
resume an endpoint with `{"is_active": false|true}` on the update route. Rotate
with:

```http
POST /v4/webhooks/{webhook_id}/rotate-secret
```

Supported events are `memory_add`, `memory_update`, `memory_delete`,
`memory_categorize`, `ingest_job_completed`,
`ingest_job_partially_completed`, `ingest_job_failed`, and
`ingest_job_cancelled`.

## Delivery contract

Requests are `POST` requests with a JSON body. The body has an
`event_details` object containing `id`, `event`, and `data`. Verify the
signature before parsing untrusted content:

```python
import hashlib
import hmac


def verify(secret: str, timestamp: str, body: bytes, header: str) -> bool:
    expected = (
        "sha256="
        + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, header)
```

Also require a recent timestamp, use `X-Memoratum-Delivery` as the
idempotency key, and acknowledge quickly. Delivery is at-least-once; the
same delivery ID can be observed more than once.

## Network policy

Production requires HTTPS and rejects loopback, private, link-local,
multicast, reserved, and metadata addresses after DNS resolution. Redirects
are disabled. Local HTTP endpoints are only accepted when **both**
`MEMORATUM_ENV=development` (or `test`) and
`MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS=true` are set. This exception is for
local development only; do not enable it on a shared or production host.

## Operations

The worker retries transient failures with exponential backoff and moves a
delivery to `dead` after the configured attempt limit. Inspect local history:

```http
GET /v4/projects/{project_id}/webhooks/deliveries
```

Replay an authorized dead/queued delivery:

```http
POST /v4/webhooks/deliveries/{delivery_id}/replay
```

Delivery rows contain IDs, status, attempt count, next retry, and a truncated
error/response status; they do not expose endpoint secrets. A production
operator should also monitor the worker's local job queue and disk usage.
