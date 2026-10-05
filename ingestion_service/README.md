# Ingestion Service

## Exception Resolution And Reprocessing (2026-10-01)

This adds `Fail -> resolve -> reprocess` to the recoverable intake/outbox loop.
Original OCR exception records and Temporal results remain immutable; operator
resolution creates a linked, independently identified processing generation.
The new generation repeats real OCR, review when required, validation and
durable delivery. Requesting a retry is not approval and cannot bypass review.

Configure a separate secret mapping `IDP_OPERATOR_API_KEYS=key:tenant,...`.
Authenticate these endpoints with `X-IDP-Operator-Key`, not `X-API-Key`.
The authenticated mapping derives the tenant; an inconsistent `X-Tenant-Id`
returns 403. Public intake credentials do not authorize operator actions.
Actor identity is a credential fingerprint, not a caller-supplied actor header.
Rotate keys using the documented operational procedure; old-key resolution
replay fingerprints include actor identity and must not be silently rewritten.

| Endpoint | Purpose |
| --- | --- |
| `GET /exceptions?limit=50&offset=0` | Tenant-scoped durable OCR exceptions and linked resolution status. |
| `GET /exceptions/{job_id}` | Original exception plus immutable resolution record. |
| `POST /exceptions/{job_id}/reprocess` | 202; requires `Idempotency-Key` and JSON `reason` (5-1000 characters). |

Example after repairing the OCR service, using a tenant's operator key:

```bash
curl -fsS "$INGESTION_URL/exceptions" -H "X-IDP-Operator-Key: $OPERATOR_KEY"
curl -fsS -X POST "$INGESTION_URL/exceptions/$FAILED_JOB_ID/reprocess" \
  -H "X-IDP-Operator-Key: $OPERATOR_KEY" \
  -H "Idempotency-Key: $RESOLUTION_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"OCR engine restored and readiness verified"}'
```

Only a terminal Temporal `ocr_exception` with its durable exception record can
be resolved. Before registration, the original raw object's tenant/job prefix,
bucket, byte length and SHA-256 are verified with a bounded read. Corrupt or
unavailable registered sources return 409; transient storage/Temporal-state
outages return 503. Neither registers a replacement or marks the exception
resolved. Foreign job access returns 404.

`ingestion_reprocess_requests` commits parent/child identity, request key,
fingerprint, actor, reason and lifecycle audit together with the new job. It
allows one immutable replacement per parent. The same key/reason/actor replays
the existing child without needing source access; a changed request returns 409.
If that child also fails OCR, resolve the child's exception for another generation
rather than mutate the original request. Replacement jobs intentionally bypass
content deduplication; ordinary intake still deduplicates as before.

Registered metadata carries `reprocess_parent_job_id`, generation and scoped
source reference. Raw bytes are copied to the new tenant/job key, then normal
verified-artifact registration and the fenced dispatch outbox start one new
Temporal execution. A crash after the resolution commit, copy or dispatch is
recovered by the existing reconciler. Integrity failures during recovery become
`REPROCESS_SOURCE_INVALID`; transient storage failures remain retryable.
202 means the request is durable, not that processing or delivery succeeded.

Resolution status starts `requested`, becomes `dispatched`, and is reconciled
to `resolved` only after the child workflow returns `delivered`. Other terminal
outcomes, including rejection, review timeout, OCR failure, delivery timeout or
artifact failure, become `needs_attention` with their outcome. Failure to read
one child's workflow does not block other resolution records.
Polling rotates bounded batches using `last_checked_at` so long-running reviews
and temporarily unreadable records do not starve later resolutions. Delivery metadata
retains parent/generation provenance. Public document status also exposes
`PENDING_DELIVERY` and `DELIVERY_EXCEPTION` while the new workflow waits.

Run this service with the existing commands below. Local startup creates the
additive resolution table; production rollout needs an approved migration,
backups, least-privilege credentials and protected operator transport. Retention
holds raw and derived data for active `requested`/`dispatched` parent-child pairs.
Needs-attention exceptions follow configured retention; do not leave them until
their source expires. The retention apply step takes maintenance locks.

Verification from the repository root:

```bash
.venv/bin/python -m unittest ingestion_service.tests.test_reprocessing -v
bash scripts/run_recovery_delivery_verification.sh
```

The live drill submits real public intake documents during an OCR outage,
resolves through authorized HTTP, kills a child process after the resolution
commit, verifies one replacement execution, then completes actual Label Studio
approval, corrected revalidation and acknowledged delivery. Corrupt source,
tenant denial and changed-key/reason conflicts are asserted. Report:
`artifacts/recovery-delivery/report.json`. Functional fixtures do not prove
representative production accuracy or deployed-image acceptance.

## Core Purpose
Accept documents from external sources, validate requests, persist raw artifacts, register metadata/audit events, and enqueue jobs into the IDP workflow.

## Scope
- Receive files from API, email connectors, and batch uploads.
- Perform request validation, deduplication, and idempotency checks.
- Persist raw artifacts and metadata.
- Enqueue jobs for `workflow_orchestrator` via Temporal.

## Primary Inputs
- Multipart file uploads (PDF, PNG, JPG, TIFF, BMP, WEBP).
- Form fields: `source`, `external_reference`.
- Headers: auth + tenant-scoping + optional idempotency.

## Primary Outputs
- Job record with unique job ID.
- Raw document artifact URI in object storage.
- Workflow queue submission metadata.
- Audit events for lifecycle traceability.

## Interfaces
- `GET /health`
- `POST /documents`
- `GET /documents/{job_id}`
- `GET /documents`
- `GET /documents/{job_id}/audit`
- `GET /documents/{job_id}/result`

Public client/API consumer traffic should enter through the Traefik gateway, not
the direct service port. See [`../infra/api/PUBLIC_API.md`](../infra/api/PUBLIC_API.md)
for the tenant-facing contract and [`../clients/python/idp_client.py`](../clients/python/idp_client.py)
for a dependency-free reference client.

## How To Run

Prerequisites:
- `postgres`, `seaweedfs`, and `temporal` must be reachable via `.env` settings.

Run with Docker Compose service:
```bash
docker compose up -d ingestion-service
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group temporal
PYTHONPATH=. .venv/bin/uvicorn ingestion_service.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:
```bash
curl -fsS http://localhost:8000/health
```


## How to run full Temporal integration/e2e against live stack:

1. Prepare env:
```bash
cp .env.example .env
```

2. Start live stack:
```bash
docker compose up -d --build 
OR
docker compose up -d ingestion-service temporal-ui
```

3. Verify health:
```bash
docker compose ps
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8088
```

4. Run e2e:
```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default ./scripts/run_e2e.sh
```

5. Confirm success:
- Script prints `E2E test completed successfully.`
- Temporal UI at [http://localhost:8088](http://localhost:8088) shows `COMPLETED` after delivery or `RUNNING` while review is pending in namespace `default`.
- Default runtime mode allows missing required fields when workflow outcome is `pending_human_review`.
- For strict extraction-quality assertions, run:
```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default STRICT_REQUIRED_FIELDS=1 ./scripts/run_e2e.sh
```


6. If it fails, inspect logs:
```bash
docker compose logs --tail=200 ingestion-service
docker compose logs --tail=200 workflow-orchestrator
docker compose logs --tail=200 preprocess-worker ocr-service layout-service extraction-service validation-service delivery-service
```

---

## Authentication and Tenant Scoping
- Accepts `X-API-Key` (or `Authorization: Bearer <token>`).
- Optional: `X-Tenant-Id`, `X-Actor-Id`.
- API key mapping is configured with `INGESTION_API_KEYS` format: `key1:tenantA,key2:tenantB`.
- If tenant header is supplied and does not match key mapping, request is rejected (`403`).

## Request Validation
- Max file size: `INGESTION_MAX_UPLOAD_SIZE_MB`.
- Allowed content types: `INGESTION_ALLOWED_MIME_TYPES`.
- Allowed extensions: `INGESTION_ALLOWED_EXTENSIONS`.
- File-signature inspection is enforced for PDF/PNG/JPEG/TIFF/BMP/WEBP.
- Empty files are rejected.

## Idempotency and Deduplication
- Idempotency is controlled by `Idempotency-Key` header.
- Replay behavior: same tenant + same idempotency key returns original job.
- Deduplication uses SHA256 of payload across a tenant window (`INGESTION_DEDUPE_WINDOW_HOURS`).
- Order of checks:
  1. Idempotency lookup.
  2. Hash-based dedupe lookup.
  3. New job creation.

## Metadata and Audit Persistence
- Metadata table: `ingestion_jobs`.
- Audit table: `ingestion_audit_events`.
- Core statuses:
  - `RECEIVED`
  - `STORED`
  - `QUEUED`
  - `FAILED`
- Workflow runtime status is also surfaced from Temporal when available.

## Queue Publish Contract
Workflow input payload published to Temporal includes:
- `job_id`
- `tenant_id`
- `source`
- `raw_bucket`
- `raw_key`

## Observability
Default HTTP metrics plus ingestion-specific metrics:
- `ingestion_submissions_total`
- `ingestion_upload_bytes_total`
- `ingestion_upload_duration_seconds`
- `ingestion_idempotency_replays_total`
- `ingestion_dedup_hits_total`
- `ingestion_queue_publish_total`
- `ingestion_auth_failures_total`
- `ingestion_audit_failures_total`
- `ingestion_failures_total`

## Failure Handling
- Validation errors return explicit 4xx codes.
- Raw artifact persistence issues return `503`; the job remains `RECEIVED` until reconciliation determines whether the upload committed.
- Queue publish failures leave a durable dispatch record and return the accepted job with status `STORED`; reconciliation retries Temporal dispatch.
- Unhandled exceptions are mapped to `500` with a safe error body.
- Error responses use a stable envelope:
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

## Security and Compliance Controls
- Tenant-scoped access for job/status/audit/result endpoints.
- Security headers enabled (`X-Content-Type-Options`, `X-Frame-Options`, `Cache-Control`).
- No raw file payloads are logged.
- Actor attribution retained via audit entries.
- Retention enforcement removes expired raw objects, derived job artifacts,
  metadata, and audit events according to independent policy windows.

## Key Configuration
- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `INGESTION_DB_POOL_MIN`
- `INGESTION_DB_POOL_MAX`
- `RAW_ARTIFACT_RETENTION_DAYS`
- `DERIVED_ARTIFACT_RETENTION_DAYS`
- `AUDIT_RETENTION_DAYS`
- `S3_ENDPOINT`
- `S3_ACCESS_KEY`
- `S3_SECRET_KEY`
- `S3_SECURE`
- `RAW_BUCKET`
- `TEMPORAL_ADDRESS`
- `TEMPORAL_NAMESPACE`
- `TEMPORAL_TASK_QUEUE`
- `INGESTION_MAX_UPLOAD_SIZE_MB`
- `INGESTION_ALLOWED_MIME_TYPES`
- `INGESTION_ALLOWED_EXTENSIONS`
- `INGESTION_DEDUPE_WINDOW_HOURS`
- `INGESTION_WORKFLOW_TIMEOUT_MINUTES`
- `INGESTION_REQUIRE_AUTH`
- `INGESTION_API_KEYS`
- `INGESTION_AUTH_HEADER_NAME`
- `INGESTION_TENANT_HEADER_NAME`
- `INGESTION_ACTOR_HEADER_NAME`

With authentication enabled, audit identity is derived from the validated API
key's tenant (`api-key:<tenant>`). `X-Actor-Id` is not trusted as an authenticated
identity. With authentication disabled, it remains an unverified local-test hint.

## Postman
Postman assets are included under [`postman/`](./postman):
- `ingestion_service.postman_collection.json`
- `ingestion_service.local.postman_environment.json`

Import both files, then run in this sequence:
1. `Health`
2. `Submit Document (New Job)`
3. `Get Document Status`
4. `Get Audit Trail`
5. `Get Workflow Result`

For idempotency checks, run `Submit Document (Idempotent)` twice with the same `idempotencyKey`.

## Non-Goals
- OCR, extraction, or document intelligence logic.
- Downstream business validation and delivery handling.

## Change Log
- 2026-05-21: Implemented full ingestion pipeline features (auth/tenant scope, validation, idempotency/dedupe, artifact persistence, metadata/audit storage, Temporal queue publish, status/audit/result APIs, security headers, and ingestion-specific observability metrics).
- 2026-07-03: Added tenant-isolation hardening evidence and backup-first retention enforcement through `scripts/enforce_retention.py`.

## 2026-09-30: Recoverable Dispatch

Registration and raw artifact upload precede an outbox entry written in the same Postgres transaction as the `STORED` state. The service tries Temporal immediately; if unavailable, it returns `STORED` and retries from the outbox with a lease and backoff. Temporal workflow IDs reject reuse, so a retry after a successful start cannot create a second completed workflow. A reconciler also checks `RECEIVED` jobs after two minutes: it registers an uploaded object or marks a missing upload failed after fifteen minutes. The pool is thread safe for concurrent API and reconciliation work. Reusing an idempotency key with different file bytes returns HTTP 409.

`GET /documents/{job_id}` reports `PENDING_HUMAN_REVIEW` while Temporal waits. `GET /documents/{job_id}/result` returns the review snapshot during that wait, then the final result after review. The default review timeout is seven days; set `INGESTION_WORKFLOW_TIMEOUT_MINUTES` longer than that (`11520` by default). Non-default tenant raw keys use `tenants/<tenant_id>/jobs/<job_id>/...`. Keep `INGESTION_API_KEYS` tenant IDs to letters, digits, `.`, `_`, and `-`.

## 2026-09-30: Page-Aware Stage Budgets

The workflow start payload now includes preprocess, OCR, and layout timeout budgets from the ingestion configuration. Preprocess uses the configured maximum page count; OCR and layout scale their activity deadlines with the actual page count. Deploy the same page cap and per-page timeout values across ingestion, orchestrator, and processing services. The document-level workflow timeout must remain longer than review wait plus processing headroom.

## 2026-10-01: Intake Recovery and Authorization Gate

The first implementation pair is **submit -> persist -> dispatch -> reconcile**
and **authorize -> scope -> deny**. No operator reprocessing or durable delivery
queue is implied by this gate; those are the next pair in
`infra/architecture/IMPLEMENTATION_ORDER.md`.

- Job registration, raw-artifact registration/outbox creation, dispatch
  acknowledgement, and recovery failure each persist their lifecycle audit in
  the same database transaction. A process crash cannot commit the transition
  without its corresponding lifecycle audit.
- A tenant/content advisory transaction lock collapses simultaneous equal-byte
  submissions to one non-failed job within `INGESTION_DEDUPE_WINDOW_HOURS`.
  Different tenants do not share jobs, even when their content and keys match.
- `ingestion_idempotency_requests` binds every supplied request key, including a
  new key on a content-deduplicated submission. Same tenant/key + same bytes
  replays the canonical job. Same tenant/key + different bytes returns `409`.
  Keys are limited to 256 characters; a failed job retains its original key.
- Schema initialization adds the request-binding table, backfills historical
  job keys, and adds `lease_token` to the outbox. Keys discarded by older
  deduplication behavior cannot be reconstructed. Do not deploy old dispatcher
  code concurrently with the fenced dispatcher; roll ingestion instances
  together after a backup and test the schema change on staging first.
- Dispatch leases last two minutes. Every claim has a new opaque token. A
  superseded worker cannot acknowledge or release another worker's lease.
  An acknowledgement lost after Temporal starts is recovered with the same
  tenant/job workflow ID and run ID, not a second workflow.
- Upload reconciliation examines `RECEIVED` jobs after two minutes. Existing
  objects must match registered byte length and SHA-256 before dispatch.
  Corruption becomes `RAW_ARTIFACT_CORRUPT`; missing objects become
  `RAW_UPLOAD_INCOMPLETE` after fifteen minutes. Transient storage failures
  preserve the job for another check. Checks run at least every five seconds
  between dispatch attempts so a busy outbox does not starve upload recovery.
- The API reads at most the configured upload limit plus one byte before
  validation. Enforce request-body limits at the gateway too; multipart parsing
  and temporary-file spooling occur before this read.
- Status, audit, and result APIs verify tenant ownership before returning data;
  an inaccessible or unknown job returns `404`. A valid key paired with a
  different tenant header returns `403`; invalid credentials return `401`.
- Internal stage calls require `X-IDP-Internal-Key` when
  `IDP_REQUIRE_INTERNAL_AUTH=true`. That key is a trusted service credential,
  not an end-user tenant token. Keep stage APIs private, transport encrypted,
  and scope public callers through ingestion. Object references reject foreign
  tenant/job prefixes, dot-segment paths, repeated slashes, backslashes, and
  percent-encoded paths.

### Run the Verification Gate

Start from the repository checkout, not the deployed release directory. Initialize
the isolated development profile as described in `infra/verification/README.md`,
then run:

```bash
bash scripts/run_ingestion_verification.sh
```

Set `IDP_DOCKER_CONTEXT` to another configured engine if needed. The runner pauses
only this profile's ingestion container while terminated child processes exercise
registration, upload, outbox, and Temporal-start crash windows. It restores intake
on exit, then checks real HTTP idempotency, concurrent deduplication, and tenant
denial across all eight processing stages. It does not kill the HTTP server at
instrumented request lines or claim real-document accuracy evidence.

Evidence: `artifacts/ingestion-recovery/report.json` and
`artifacts/ingestion-recovery/tenant-boundaries.json`. Both must have `passed: true`.
The verification profile uses local fixture credentials and separate Compose
resources; it is not a staging promotion or a replacement for production backup,
restore, retention, or quality acceptance.

Offline tests:

```bash
.venv/bin/python -m unittest ingestion_service.tests.test_dispatch \
  ingestion_service.tests.test_auth tests.test_upload_recovery \
  tests.test_tenant_scope tests.client_api.test_public_contract -v
```
