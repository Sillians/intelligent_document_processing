# Validation Service

## Core Purpose
Apply confidence and rule-based gates to extracted document fields before the pipeline decides whether a document can move to delivery or must go to human review.

## Implemented Core Functionalities
- Accepts extraction results from the workflow orchestrator at `POST /validate`.

- Loads the persisted extraction artifact when `extraction_bucket` is provided, so validation can use route, field confidence, and source metadata.

- Falls back to the request body when the extraction artifact is unavailable.

- Infers route from field names when no explicit route is available.

- Applies route-specific required-field, recommended-field, date-format, money-format, document-confidence, field-confidence, and LLM-fallback policy checks.

- Produces a pipeline-compatible decision: `auto_approved`, `needs_review`, or `rejected`.

- Returns `requires_human_review` and reason codes for the Temporal branch to human review.

- Persists validation decisions to object storage for auditability.

- Runs validation work in a bounded worker pool with timeout and busy-worker handling.


## Supported Routes
- `invoice`
- `receipt`
- `contract`
- `purchase_order`
- `bank_statement`
- `generic`

## Primary Inputs
- `job_id`
- `extraction_key`
- `extraction_bucket` optional, but recommended
- `fields`
- `confidence`
- `used_vlm_fallback`
- `route` optional

Example request:
```json
{
  "job_id": "job-123",
  "extraction_bucket": "extraction-artifacts",
  "extraction_key": "jobs/job-123/extraction/result.json",
  "fields": {
    "invoice_number": "INV-001",
    "invoice_date": "2026-06-01",
    "total_amount": "$500.00"
  },
  "confidence": 0.95,
  "used_vlm_fallback": false,
  "route": "invoice"
}
```

## Primary Outputs
- `verdict`
- `requires_human_review`
- `reasons`
- `warnings`
- `route`
- `confidence`
- `rule_results`
- `validation_bucket`
- `validation_key`

Example response:
```json
{
  "job_id": "job-123",
  "verdict": "auto_approved",
  "requires_human_review": false,
  "reasons": [],
  "warnings": ["missing_recommended_fields:currency,vendor_name"],
  "route": "invoice",
  "confidence": 0.95,
  "used_vlm_fallback": false,
  "validation_bucket": "validation-artifacts",
  "validation_key": "jobs/job-123/validation/result.json"
}
```

## Interfaces
- `GET /health`
- `POST /validate`

## Rule Behavior
- Missing required fields always route to `review`.
- Invalid configured date or money fields route to `review`.
- Document confidence below `AUTO_APPROVE_THRESHOLD` routes to `review`.
- Required field confidence below `VALIDATION_MIN_FIELD_CONFIDENCE` routes to `review` when field confidences are available.
- LLM/VLM fallback output routes to `review` for strict business document routes by default.
- Empty or very low-confidence extractions are marked `rejected`, while still returning `requires_human_review=true` for the existing workflow branch.

## Key Configuration
- `VALIDATION_BUCKET`
- `VALIDATION_PROFILE`
- `VALIDATION_POLICY_VERSION`
- `VALIDATION_REQUEST_TIMEOUT_SECONDS`
- `VALIDATION_MAX_INFLIGHT_REQUESTS`
- `VALIDATION_MIN_FIELD_CONFIDENCE`
- `VALIDATION_AUTO_REJECT_THRESHOLD`
- `VALIDATION_PERSIST_DECISION`
- `AUTO_APPROVE_THRESHOLD`
- `EXTRACTION_BUCKET`

## Data and Storage
- Primary artifact bucket: `VALIDATION_BUCKET`.
- Fallback artifact bucket, if primary write fails: `EXTRACTION_BUCKET`.
- Artifact key pattern:
```text
jobs/<job_id>/validation/result.json
```

## Failure Handling
- Busy worker pool returns HTTP `503` so Temporal can retry.
- Request timeout returns HTTP `504`.
- Extraction artifact load failures do not fail validation; the service validates the request body instead.
- Primary artifact upload falls back to `EXTRACTION_BUCKET`.

## Security and Compliance
- Validation artifacts preserve rule decisions and policy version for auditability.
- Logs avoid dumping full extracted field payloads.
- Rule outputs use stable reason codes for downstream review, reporting, and compliance controls.

## Observability
- FastAPI app is instrumented with the shared Prometheus middleware.
- Rule reason codes can be aggregated into manual-review and rejection-rate dashboards.

## How To Run

Run with Docker Compose service:
```bash
docker compose up -d validation-service
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group base
PYTHONPATH=. .venv/bin/uvicorn validation_service.app.main:app --host 0.0.0.0 --port 8015 --reload
```

Run tests:
```bash
.venv/bin/python -m unittest discover -s validation_service/tests -p 'test_*.py' -v
```

Run the focused workflow contract tests:
```bash
.venv/bin/python -m unittest workflow_orchestrator.tests.test_pipeline -v
```

## Non-Goals
- OCR and image processing.
- Correcting extracted values using LLMs.
- Human-review UI creation.
- Final artifact delivery.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations.
- 2026-06-03: Implemented route-aware validation, confidence gating, rule reason codes, artifact persistence, bounded worker execution, tests, and run instructions.
