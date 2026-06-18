# OCR Service

## Core Purpose
Convert processed document images into machine-readable text with coordinates and confidence.

## Scope
- Load and run PaddleOCR with version-compatible init/inference paths.
- Extract token text, bounding geometry, and confidence scores.
- Emit `full_text`, token count, and mean confidence for downstream stages.
- Apply deterministic fallback payloads when OCR runtime fails.
- Run blocking OCR workload in a bounded worker pool to avoid blocking FastAPI event loop.
- Guard request execution with timeout and busy-slot fallback to prevent pipeline stalls.

## Primary Inputs
- `job_id`
- `preprocessed_key`

## Primary Outputs
- `ocr_key`
- `token_count`
- `mean_confidence`
- `full_text`
- `fallback_used`

## Interfaces
- `GET /health`
- `POST /ocr`

## Upstream Dependencies
- `preprocess_worker`.
- Model runtime (CPU/GPU inference backend).

## Downstream Consumers
- `layout_service`.
- `extraction_service`.
- `evaluation_service`.

## Data and Storage
- OCR artifact bucket: `OCR_BUCKET`
- Key pattern: `jobs/<job_id>/ocr/ocr.json`

## Key Configuration
- `OCR_LANGUAGE`
- `OCR_DISABLE_MKLDNN`
- `OCR_ENGINE_TIMEOUT_SECONDS`
- `OCR_ENGINE_LOCK_TIMEOUT_SECONDS`
- `OCR_REQUEST_TIMEOUT_SECONDS`
- `OCR_MAX_INFLIGHT_REQUESTS`
- `OCR_FORCE_FALLBACK`

## SLI / SLO Targets
- OCR service availability >= 99.5%.
- p95 per-page OCR latency <= 1.2s (engine-dependent).

## Failure Handling
- OCR engine initialization/runtime errors are caught and logged.
- OCR request execution runs in an isolated worker pool with timeout guard to avoid hanging workflow stages.
- If worker slots are saturated, service returns deterministic fallback immediately (`service_busy`) instead of queueing indefinitely.
- When `OCR_FORCE_FALLBACK=true`, OCR service skips Paddle runtime and always emits deterministic fallback token for stable functional-pipeline validation.
- Fallback token payload is returned when OCR cannot be produced, so downstream workflow stages can continue and route to review when needed.

## Security and Compliance
- Restrict model access by tenant/project policy.
- Log model version used for each output.

## Observability
- Metrics: CER proxy, confidence distributions, latency, fallback rate.
- Traces: model call span with engine/version tags.

## How To Run

Run with Docker Compose service:
```bash
docker compose up -d ocr-service
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group ocr
PYTHONPATH=. .venv/bin/uvicorn ocr_service.app.main:app --host 0.0.0.0 --port 8011 --reload
```

Run tests:
```bash
.venv/bin/python -m unittest discover -s ocr_service/tests -p 'test_*.py' -v
```

## Non-Goals
- Layout reasoning and business field mapping.


## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations. No runtime code changes were applied to this service in this update.
- 2026-05-26: Added PaddleOCR v3-compatible execution handling and deterministic fallback token behavior to avoid hard 500 failures when OCR runtime calls fail.
- 2026-05-27: Added stable PaddleOCR initialization strategy (`MKLDNN`-safe path), cross-version inference API fallback, OCR timeout guard, normalized token text handling, richer OCR payload fields, and unit tests.
- 2026-05-27: Moved OCR request path off async event loop into bounded worker pool, added inflight slot controls, request-timeout fallback behavior, and updated tests.
- 2026-05-27: Added `OCR_FORCE_FALLBACK` mode to allow deterministic non-Paddle execution in constrained local environments while preserving full Paddle path when disabled.
