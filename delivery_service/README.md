# Delivery Service

## Core Purpose
Publish approved structured document results to downstream destinations reliably, idempotently, and with a durable delivery audit trail.

The service closes the primary automated IDP pipeline:

```text
Ingestion
-> Preprocessing
-> OCR
-> Layout
-> Classification
-> Extraction
-> Validation
-> Delivery when auto-approved
```

Documents requiring review stop at `human-review-console`. The delivery service already accepts `human_approved` as an approval status for a future review-resolution callback or resume workflow.

## Implemented Core Functionalities
- Accepts approved delivery requests at `POST /deliver`.
- Rejects unapproved requests by default.
- Creates deterministic delivery IDs for Temporal retry idempotency.
- Detects completed deliveries and returns the existing receipt without publishing twice.
- Packages results in a versioned delivery envelope.
- Supports configurable field redaction before outbound delivery.
- Uses a modular destination-provider interface.
- Delivers to S3-compatible object storage by default.
- Supports optional signed webhook delivery.
- Retries transient provider failures with exponential backoff.
- Persists successful and failed delivery receipts.
- Exposes a delivery receipt lookup API.
- Runs destination providers concurrently when multiple destinations are selected.
- Bounds concurrent delivery requests and returns retryable overload responses.

## Provider Model
A destination provider implements the following interface:

```python
class DeliveryProvider(Protocol):
    name: str

    async def deliver(self, context: DeliveryContext) -> dict:
        ...
```

Current providers:
- `ObjectStorageProvider`: writes the versioned final payload to `DELIVERY_BUCKET`.
- `WebhookProvider`: sends the versioned final payload to an HTTP endpoint with idempotency and optional HMAC signature headers.

New providers can be registered in `delivery_service/app/providers.py` for message queues, databases, REST APIs, enterprise systems, or custom sinks without changing the core delivery orchestration logic.

## Primary Inputs
- `job_id`
- `payload`
- `approval_status`: normally `auto_approved` or `human_approved`
- `destinations` optional list of provider names
- `webhook_url` optional per-request webhook override
- `idempotency_key` optional explicit idempotency key
- `extraction_bucket` optional
- `extraction_key` optional
- `validation_bucket` optional
- `validation_key` optional
- `metadata` optional


Example request:

```json
{
  "job_id": "job-123",
  "payload": {
    "invoice_number": "INV-001",
    "invoice_date": "2026-06-01",
    "total_amount": "$500.00"
  },
  "approval_status": "auto_approved",
  "destinations": ["object_storage"],
  "extraction_bucket": "extraction-artifacts",
  "extraction_key": "jobs/job-123/extraction/result.json",
  "validation_bucket": "validation-artifacts",
  "validation_key": "jobs/job-123/validation/result.json",
  "metadata": {
    "route": "invoice"
  }
}
```

## Primary Outputs
- `job_id`
- `delivery_id`
- `delivery_status`
- `delivery_artifact`
- `delivery_bucket`
- `delivery_receipt_key`
- `destination_results`
- `webhook_status`
- `delivered_at`
- `idempotent_replay`

Example response:

```json
{
  "job_id": "job-123",
  "delivery_id": "delivery-abc123",
  "delivery_status": "success",
  "delivery_artifact": "s3://delivery-artifacts/jobs/job-123/delivery/delivery-abc123/payload.json",
  "delivery_bucket": "delivery-artifacts",
  "delivery_receipt_key": "jobs/job-123/delivery/delivery-abc123/receipt.json",
  "destination_results": [
    {
      "provider": "object_storage",
      "status": "success",
      "attempts": 1
    }
  ],
  "webhook_status": "skipped",
  "idempotent_replay": false
}
```

## Interfaces
- `GET /health`
- `POST /deliver`
- `GET /deliveries/{job_id}/{delivery_id}`
- API documentation: `http://localhost:8017/docs`

## Approval Gate
Delivery requires explicit approval by default:

```env
DELIVERY_REQUIRE_APPROVAL=true
DELIVERY_ALLOWED_APPROVAL_STATUSES=auto_approved,human_approved
```

Requests without an allowed `approval_status` return HTTP `409`. The workflow orchestrator passes the validation verdict and validation artifact references into the delivery request.

Do not disable the approval gate in production unless an equivalent upstream enforcement mechanism exists.

## Idempotency
When `idempotency_key` is provided, it controls the deterministic delivery ID. Otherwise, the service derives the delivery ID from:
- Job ID
- Approved payload
- Approval status
- Selected destinations
- Webhook URL

A successful existing receipt causes the service to return the prior result with:

```json
{
  "idempotent_replay": true
}
```

This prevents duplicate downstream delivery when Temporal retries an activity.

## Object Storage Provider
Default configuration:

```env
DELIVERY_PROVIDERS=object_storage
DELIVERY_BUCKET=delivery-artifacts
```

Payload artifact pattern:

```text
jobs/<job_id>/delivery/<delivery_id>/payload.json
```

Receipt artifact pattern:

```text
jobs/<job_id>/delivery/<delivery_id>/receipt.json
```

If receipt persistence to `DELIVERY_BUCKET` fails, the service falls back to `VALIDATION_BUCKET`.

## Webhook Provider
Enable final payload delivery to a webhook destination globally:

```env
DELIVERY_PROVIDERS=object_storage,webhook
DELIVERY_WEBHOOK_URL=https://consumer.example.com/idp/results
DELIVERY_WEBHOOK_SECRET=<strong-shared-secret>
```

A caller can also select destinations and provide a webhook URL per request:

```json
{
  "destinations": ["object_storage", "webhook"],
  "webhook_url": "https://consumer.example.com/idp/results"
}
```

Per-request webhook URLs are disabled by default to prevent server-side request forgery:

```env
DELIVERY_ALLOW_REQUEST_WEBHOOK_URL=false
```

If per-request destinations are required, explicitly enable them and restrict destination hosts:

```env
DELIVERY_ALLOW_REQUEST_WEBHOOK_URL=true
DELIVERY_WEBHOOK_ALLOWED_HOSTS=consumer.example.com,backup-consumer.example.com
```

Webhook headers:
- `Idempotency-Key`: deterministic delivery ID
- `X-IDP-Delivery-Id`: deterministic delivery ID
- `X-IDP-Job-Id`: source job ID
- `X-IDP-Signature-256`: `sha256=<hex digest>` when `DELIVERY_WEBHOOK_SECRET` is configured

Consumers should verify the HMAC SHA-256 signature over the exact raw request body before accepting the payload.

Webhook signatures are required by default:

```env
DELIVERY_WEBHOOK_REQUIRE_SIGNATURE=true
```

## Workflow Event Webhooks
The workflow orchestrator also posts terminal client events to this service at
`POST /webhooks/events`. The delivery service signs, retries, and persists the
outbound event receipt.

Emitted event types:

- `document.completed`
- `document.pending_human_review`
- `document.failed`

Event receipt artifact pattern:

```text
jobs/<job_id>/webhooks/<event_id>/receipt.json
```

If `DELIVERY_WEBHOOK_URL` is empty, event delivery is skipped but an audit
receipt is still written. Consumers should deduplicate events by `event_id` and
verify `X-IDP-Signature-256` when signatures are enabled.

## Redaction
Configure top-level or nested field names that must be redacted from outbound payloads:

```env
DELIVERY_REDACT_FIELDS=account_number,ssn,national_id
```

Every matching dictionary key is replaced with `[REDACTED]`. The delivery envelope and receipt include the list of redacted field names.

## Key Configuration
- `DELIVERY_BUCKET`
- `DELIVERY_PROVIDERS`
- `DELIVERY_REQUIRE_APPROVAL`
- `DELIVERY_ALLOWED_APPROVAL_STATUSES`
- `DELIVERY_REQUEST_TIMEOUT_SECONDS`
- `DELIVERY_MAX_INFLIGHT_REQUESTS`
- `DELIVERY_RETRY_MAX`
- `DELIVERY_RETRY_BACKOFF_SECONDS`
- `DELIVERY_PAYLOAD_FORMAT_VERSION`
- `DELIVERY_REDACT_FIELDS`
- `DELIVERY_WEBHOOK_URL`
- `DELIVERY_WEBHOOK_SECRET`
- `DELIVERY_WEBHOOK_REQUIRE_SIGNATURE`
- `DELIVERY_ALLOW_REQUEST_WEBHOOK_URL`
- `DELIVERY_WEBHOOK_ALLOWED_HOSTS`
- `DELIVERY_WEBHOOK_TIMEOUT_SECONDS`

## Failure Handling
- Unknown providers return HTTP `422`.
- Missing or invalid approval status returns HTTP `409`.
- Busy delivery capacity returns HTTP `503` so Temporal can retry.
- Request timeout returns HTTP `504`.
- Provider failure returns HTTP `502` after configured retries.
- Successful and failed delivery attempts are persisted as receipts.
- Temporal activity retry provides an additional retry layer outside the service.

## Security and Compliance
- Approval status is required before delivery.
- Webhooks can be signed with `HMAC SHA-256`.
- Arbitrary per-request webhook URLs are disabled by default and can be restricted to an explicit host allowlist.
- Configured sensitive fields are redacted before outbound publishing.
- Delivery payloads and receipts preserve source extraction and validation artifact references.
- Secrets should be injected by a secret manager and must not be committed.

## Observability
- `FastAPI` app is instrumented with the shared Prometheus middleware.
- Delivery receipts include provider attempts, duration, destinations, status, and failure details.
- Useful operational metrics include success rate, failure rate, retry count, destination latency, and idempotent replay rate.

## How To Run

Run with Docker Compose:

```bash
docker compose up -d delivery-service
```

Run locally from repository root:

```bash
uv sync --frozen --no-default-groups --group base
PYTHONPATH=. .venv/bin/uvicorn delivery_service.app.main:app --host 0.0.0.0 --port 8017 --reload
```

Run tests:

```bash
.venv/bin/python -m unittest discover -s delivery_service/tests -p 'test_*.py' -v
```

Run focused workflow contract tests:

```bash
.venv/bin/python -m unittest workflow_orchestrator.tests.test_pipeline -v
```

## Non-Goals
- Performing extraction or validation.
- Human-review task creation.
- Managing downstream consumer business logic.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations.
- 2026-06-04: Implemented approval-gated modular delivery providers, object storage delivery, signed webhooks, idempotency, redaction, retries, durable receipts, status lookup, tests, and workflow contract integration.
