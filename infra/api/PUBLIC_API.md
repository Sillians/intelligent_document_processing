# Public IDP API Contract

The public edge is the API gateway. Clients should not call internal service
ports directly in production.

```text
Client/API consumer
  -> Traefik gateway
  -> ingestion-service
  -> Temporal workflow
  -> internal IDP services
```

## Public Routes

The gateway routes only these public paths to `ingestion-service`:

- `GET /`
- `GET /health`
- `POST /documents`
- `GET /documents/{job_id}`
- `GET /documents/{job_id}/result`

All other service paths stay internal to the Compose network. The service has
operator-oriented routes such as list and audit, but the gateway does not expose
them as part of the public client contract.

Use the gateway URL as the client base URL:

```text
http://localhost:8081
https://<production-idp-host>
```

Opening the base URL in a browser or with `curl http://localhost:8081/`
returns a small JSON API index. Client integrations should still call the
specific document endpoints.

## Authentication

API key validation is enforced by `ingestion-service`.

Required client headers:

```http
X-API-Key: <tenant-api-key>
X-Tenant-Id: <tenant-id>
X-Actor-Id: <user-or-system-id>
Idempotency-Key: <stable-client-request-id>
X-Request-ID: <optional-client-correlation-id>
```

Traefik OSS does not validate arbitrary API keys or JWTs by itself. If JWT
validation is required at the gateway later, add a forward-auth service and wire
it as a Traefik middleware.

## Submit A Document

```http
POST /documents HTTP/1.1
Content-Type: multipart/form-data
X-API-Key: <tenant-api-key>
X-Tenant-Id: default
X-Actor-Id: client-system
Idempotency-Key: invoice-123-upload-v1
```

Response:

```json
{
  "job_id": "uuid",
  "status": "QUEUED",
  "workflow_id": "idp-default-uuid",
  "workflow_run_id": "temporal-run-id",
  "artifact_uri": "s3://raw-documents/jobs/uuid/raw/document.pdf",
  "idempotency_replay": false,
  "deduplicated": false,
  "status_url": "/documents/uuid"
}
```

## Poll Status

```http
GET /documents/{job_id}
X-API-Key: <tenant-api-key>
X-Tenant-Id: default
```

Terminal workflow states include `COMPLETED`, `FAILED`, `TERMINATED`,
`CANCELED`, and `TIMED_OUT`.

## Fetch Result

```http
GET /documents/{job_id}/result
X-API-Key: <tenant-api-key>
X-Tenant-Id: default
```

Successful results end in one of two business branches:

- `delivered`
- `pending_human_review`

## Gateway Controls

Traefik currently provides:

- TLS termination on the `websecure` entrypoint.
- HTTP local gateway entrypoint for development.
- Path allowlisting for `/`, `/documents*`, and `/health`.
- Request body limit through buffering middleware.
- Rate limiting middleware.
- Security response headers.
- JSON access logs.
- Healthcheck through Traefik ping.

`ingestion-service` currently provides:

- API key and tenant validation.
- Upload MIME type, extension, and size validation.
- Idempotency handling.
- Workflow submission and status/result APIs.

Future gateway upgrades:

- Forward-auth middleware for JWT/OIDC validation.
- Per-tenant rate limits backed by an auth service.
- Canary routing to a second ingestion deployment.
- WAF in front of Traefik, or a dedicated gateway/WAF appliance.

## Error Format

All API errors use this envelope:

```json
{
  "error": {
    "code": "missing_api_credentials",
    "message": "Missing API credentials",
    "status": 401,
    "request_id": "req-123"
  }
}
```

Validation errors include `details`:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed",
    "status": 422,
    "request_id": "req-123",
    "details": [
      {
        "loc": ["body", "file"],
        "msg": "Field required",
        "type": "missing"
      }
    ]
  }
}
```

Clients should log `request_id` and include it in support tickets. If clients
send `X-Request-ID`, the API returns the same value. Otherwise the service
generates one.

## Retry And Idempotency

Clients should send a stable `Idempotency-Key` for every document submission.
Use a key derived from the tenant-side operation, not a random value per retry.

Safe retry cases:

- Network timeout before receiving a response.
- `502`, `503`, or `504` from the gateway or API.
- Client process crash after upload.

Do not retry indefinitely. Use exponential backoff with jitter and preserve the
same `Idempotency-Key`.

## Reference Client

A small dependency-free Python client is provided:

```bash
python3 clients/python/idp_client.py \
  samples/documents/sample_invoice_001.png \
  --base-url http://localhost:8081 \
  --api-key dev-ingestion-key \
  --tenant-id default \
  --actor-id local-client \
  --idempotency-key sample-invoice-001
```

For application code:

```python
from pathlib import Path

from clients.python.idp_client import IdpClient

client = IdpClient(
    base_url="http://localhost:8081",
    api_key="dev-ingestion-key",
    tenant_id="default",
    actor_id="billing-importer",
)

submission = client.submit_document(
    Path("samples/documents/sample_invoice_001.png"),
    idempotency_key="invoice-001-upload-v1",
)
status = client.wait_for_completion(submission["job_id"])
result = client.get_result(submission["job_id"])
```

## Webhooks

Polling remains the required fallback contract, but the workflow now emits
client-facing webhook events for terminal business outcomes when
`DELIVERY_WEBHOOK_URL` is configured.

Events:

- `document.completed`: document completed and final output was delivered.
- `document.pending_human_review`: document needs a human review task.
- `document.failed`: workflow failed before producing a final business result.

Webhook payload:

```json
{
  "schema_version": "1.0",
  "event_id": "evt-deterministic-id",
  "event_type": "document.completed",
  "occurred_at": "2026-06-15T00:00:00+00:00",
  "tenant_id": "default",
  "job_id": "uuid",
  "workflow_id": "idp-default-uuid",
  "data": {
    "status": "delivered",
    "classification": {},
    "validation": {},
    "delivery": {}
  }
}
```

Webhook headers:

- `Idempotency-Key`: same value as `event_id`.
- `X-IDP-Event-Id`: deterministic event ID for consumer dedupe.
- `X-IDP-Event-Type`: event name.
- `X-IDP-Job-Id`: source job ID.
- `X-IDP-Tenant-Id`: tenant ID.
- `X-IDP-Signature-256`: `sha256=<hex>` HMAC over the exact raw body when `DELIVERY_WEBHOOK_SECRET` is configured.

Consumers should verify the HMAC signature before accepting the payload, process
events idempotently by `event_id`, return any `2xx` status to acknowledge
delivery, and continue supporting `GET /documents/{job_id}` plus
`GET /documents/{job_id}/result` for recovery.

Delivery behavior:

- Webhook attempts use `DELIVERY_RETRY_MAX`,
  `DELIVERY_RETRY_BACKOFF_SECONDS`, and `DELIVERY_WEBHOOK_TIMEOUT_SECONDS`.
- Successful receipts are replayed idempotently.
- If no webhook destination is configured, the event is recorded as `skipped`.
- Receipts are persisted under `jobs/{job_id}/webhooks/{event_id}/receipt.json`.
- Per-request webhook URLs are disabled by default. If enabled, hosts must match
  `DELIVERY_WEBHOOK_ALLOWED_HOSTS`.
