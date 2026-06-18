# Ingestion Service

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
- Temporal UI at [http://localhost:8088](http://localhost:8088) shows workflow `COMPLETED` in namespace `default`.
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
- Raw artifact persistence issues return `503` and mark job `FAILED`.
- Queue publish failures return `503` and mark job `FAILED`.
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

## Key Configuration
- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `INGESTION_DB_POOL_MIN`
- `INGESTION_DB_POOL_MAX`
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
