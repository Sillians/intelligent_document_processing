# Layout Service

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
