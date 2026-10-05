# Layout Service

## Independent Page Layout (2026-10-03)
`POST /layout` accepts optional `page_slot` (1..1000), requiring one page and an
OCR artifact with matching tenant/job/slot identity. Outputs use
`layout/pages/0001.json`, etc. Local tokens and zones retain page-image pixel
coordinates; aggregation remaps page numbers, block IDs and document reading order
only after all page sources and identities validate. Existing unslotted requests
remain supported. A partial layout result cannot approve a complete document.

Use existing run commands below. Run
`.venv/bin/python -m unittest layout_service.tests.test_main tests.test_page_execution -v`.
The live page drill and history replay commands are documented in the verification
README. Layout backend quality and representative zone/evidence labels remain
separate acceptance work; structural aggregation does not prove model accuracy.

## Core Purpose
Infer document structure and reading order for context-aware extraction.

## Scope
- Detect layout regions (titles, paragraphs, tables, key-value blocks).
- Build reading order and hierarchy graphs.
- Emit table/form candidates and block relationships.

## Primary Inputs
- Page images.
- OCR tokens with bounding boxes.

## Primary Outputs
- Structured layout JSON with region labels and geometry.
- Reading-order and parent-child links.

## Interfaces
- Internal API: `POST /layout/analyze`.
- Batch queue worker mode for large jobs.

## Upstream Dependencies
- `ocr_service`.
- Layout model artifacts/runtime.

## Downstream Consumers
- `classifier_router_service`.
- `extraction_service`.
- `evaluation_service`.

## Data and Storage
- Layout annotation artifacts by page.
- Optional model feature cache.

## Key Configuration
- `LAYOUT_MODEL_VERSION`
- `MIN_REGION_CONFIDENCE`
- `TABLE_DETECTION_ENABLED`

## SLI / SLO Targets
- Region detection availability >= 99.0%.
- p95 page analysis latency <= 800ms.

## Failure Handling
- Fallback to OCR reading-order heuristics.
- Return partial layout with explicit confidence flags.

## Security and Compliance
- Enforce tenant-scoped artifact access.

## Observability
- Metrics: region counts, low-confidence rate, table detection hit rate.
- Error taxonomy for model/runtime failures.

## Non-Goals
- Business validation and external delivery.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations. No runtime code changes were applied to this service in this update.

## 2026-09-30: Tenant Scope

`POST /layout` carries `tenant_id`, verifies that preprocessing and OCR keys belong to the same tenant/job, and writes the layout artifact under that scope. Default-tenant keys retain the prior `jobs/<job_id>/...` format. Set `IDP_REQUIRE_INTERNAL_AUTH=true` and the shared `IDP_INTERNAL_API_KEY` in deployed environments.
# Page-Aware Contract (2026-09-30)

Layout now pairs each preprocessed image with the corresponding OCR page and rejects count/order mismatches. Blocks retain page-local boxes and receive `page_number`; flattened blocks have globally unique IDs and reading order. The artifact contains ordered `pages`, document `blocks`, `page_count`, `schema_version=2.0`, and aggregate text. A missing or unreadable page fails rather than emitting a fabricated layout.
`LAYOUT_REQUEST_TIMEOUT_SECONDS` is treated as a per-page budget and multiplied by the manifest page count; HTTP and Temporal deadlines must exceed it.

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
