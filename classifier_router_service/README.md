# Classifier Router Service

## Core Purpose
Classify each document into a document type and return a routing profile that tells downstream extraction which strategy to use.

## Implemented Core Functionalities
- Loads OCR artifact text from `OCR_BUCKET`.
- Optionally accepts `layout_key` and folds lightweight layout features into the decision artifact.
- Applies deterministic, explainable routing rules for:
  - `invoice`
  - `receipt`
  - `contract`
  - `purchase_order`
  - `bank_statement`
  - `generic`
- Returns confidence, confidence band, matched signal count, extraction mode, and strategy profile.
- Routes OCR fallback or weak-text documents to `generic` with `requires_review=true`.
- Persists a routing decision artifact when `CLASSIFIER_PERSIST_DECISION=true`.
- Runs blocking artifact reads/writes in a bounded worker pool so the FastAPI event loop stays responsive.

## Primary Inputs
- `job_id`
- `ocr_key`
- `layout_key` optional

## Primary Outputs
- `route`
- `strategy_profile`
- `extraction_mode`
- `classification_confidence`
- `confidence_band`
- `auto_route`
- `requires_review`
- `classification_key` when persistence is enabled

## Interfaces
- `GET /health`
- `POST /route`

Example request:
```json
{
  "job_id": "job-123",
  "ocr_key": "jobs/job-123/ocr/ocr.json",
  "layout_key": "jobs/job-123/layout/layout.json"
}
```

## Routing Profiles
- `invoice_v1`: deterministic invoice extraction first, VLM fallback if needed.
- `receipt_v1`: deterministic receipt extraction first, VLM fallback if needed.
- `contract_v1`: layout-aware VLM extraction.
- `purchase_order_v1`: deterministic purchase-order extraction first, VLM fallback if needed.
- `bank_statement_v1`: table-aware VLM extraction.
- `generic_v1`: layout-aware VLM extraction and review-biased handling.

## Key Configuration
- `CLASSIFIER_AUTO_ROUTE_THRESHOLD`
- `CLASSIFIER_MIN_TEXT_CHARS`
- `CLASSIFIER_REQUEST_TIMEOUT_SECONDS`
- `CLASSIFIER_MAX_INFLIGHT_REQUESTS`
- `CLASSIFIER_PERSIST_DECISION`

## Data and Storage
- Reads OCR artifacts from `OCR_BUCKET`.
- Reads optional layout artifacts from `LAYOUT_BUCKET`.
- Persists classification artifacts to `LAYOUT_BUCKET` under:
```text
jobs/<job_id>/classification/route.json
```

## Failure Handling
- OCR fallback text routes to `generic` and requires review.
- Insufficient text routes to `generic` and requires review.
- Busy worker pool returns HTTP `503` so Temporal can retry.
- Request timeout returns HTTP `504`.
- Missing optional layout artifact does not fail classification.

## How To Run

Run with Docker Compose:
```bash
docker compose up -d classifier-router-service
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group base
PYTHONPATH=. .venv/bin/uvicorn classifier_router_service.app.main:app --host 0.0.0.0 --port 8013 --reload
```

Run tests:
```bash
.venv/bin/python -m unittest discover -s classifier_router_service/tests -p 'test_*.py' -v
```

## Non-Goals
- Field-level extraction.
- Model training.
- Human review task creation.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations.
- 2026-06-02: Implemented functional classifier-router service with explainable deterministic routing profiles, OCR fallback handling, decision persistence, bounded worker-pool execution, configuration controls, and unit tests.

## 2026-09-30: Tenant Scope

`POST /route` carries `tenant_id`, rejects OCR or layout references outside its tenant/job scope, and stores route decisions in the scoped artifact prefix. The classifier no longer receives synthetic OCR text from orchestrator fallback. Deployed instances should require `X-IDP-Internal-Key` via `IDP_REQUIRE_INTERNAL_AUTH=true` and `IDP_INTERNAL_API_KEY`.
# Page-Aware Input (2026-09-30)

Classification consumes the document-level flattened OCR tokens, including all pages, and now returns `duration_ms` so benchmark evidence can distinguish lightweight compute from document-stage work. It does not make page-local decisions; ambiguous mixed-type documents still require review.

## 2026-10-01: Tenant Boundary Verification

This service's processing endpoint is included in the live intake/authorization
acceptance gate. A call without its internal service credential returns `401`;
authorized calls referring to another tenant/job's artifacts return `403` before
processing those objects. Review uses its dedicated tenant/worker credentials
for decisions and also rejects an operator key bound to another tenant.

Run `bash scripts/run_ingestion_verification.sh` from the repository root. Inspect
`artifacts/ingestion-recovery/tenant-boundaries.json`. The profile is isolated,
uses local fixture credentials, and leaves its services running after the test.
Keep processing APIs private: a shared internal key authenticates a trusted
service, not an end-user tenant principal. Public tenant access is mediated by
ingestion. See `infra/architecture/IMPLEMENTATION_ORDER.md` for the next exception,
delivery, page-execution, and representative-quality milestones; this gate does
not mark those implementations complete.
