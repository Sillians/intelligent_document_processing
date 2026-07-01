# Preprocess Worker

## Core Purpose
Enhance document images to improve OCR quality and downstream model accuracy.

## Scope
- Decode raw image artifacts from object storage.
- Normalize page size for stable OCR latency.
- Apply configurable denoise, CLAHE, deskew, adaptive thresholding, and median blur.
- Persist a preprocessed page artifact and transformation metadata.
- Bound concurrent requests and run blocking OpenCV/S3 work outside the FastAPI event loop.
- Fall back safely to passthrough/original image when preprocessing fails.

## Primary Inputs
- `job_id`
- `raw_bucket`
- `raw_key`

## Primary Outputs
- `preprocessed_bucket`
- `preprocessed_key`
- `metadata` (`deskew_angle`, `resize_scale`, `pipeline`, dimensions, foreground count, duration)

## Interfaces
- `GET /health`
- `POST /preprocess`

## Upstream Dependencies
- `workflow_orchestrator`.
- S3-compatible object storage (`seaweedfs` in local stack).

## Downstream Consumers
- `ocr_service`.
- `evaluation_service` (for pipeline diagnostics).

## Data and Storage
- Writes preprocessed artifact to `PREPROCESSED_BUCKET`.
- Default key pattern: `jobs/<job_id>/preprocessed/page-0001.png`.
- Non-image passthrough key: `jobs/<job_id>/preprocessed/source.bin`.

## Key Configuration
- `PREPROCESS_MAX_DIMENSION`
- `PREPROCESS_DENOISE_H`
- `PREPROCESS_THRESHOLD_BLOCK_SIZE`
- `PREPROCESS_THRESHOLD_C`
- `PREPROCESS_ENABLE_CLAHE`
- `PREPROCESS_CLAHE_CLIP_LIMIT`
- `PREPROCESS_CLAHE_TILE_GRID_SIZE`
- `PREPROCESS_ENABLE_DESKEW`
- `PREPROCESS_ENABLE_THRESHOLD`
- `PREPROCESS_MEDIAN_BLUR_KERNEL`
- `PREPROCESS_DESKEW_MIN_FOREGROUND_PIXELS`
- `PREPROCESS_DESKEW_MAX_ANGLE` (default `15`; larger estimated corrections are
  rejected rather than rotating the page destructively)
- `PREPROCESS_REQUEST_TIMEOUT_SECONDS`
- `PREPROCESS_MAX_INFLIGHT_REQUESTS`

## SLI / SLO Targets
- Preprocess failure rate <= 0.5%.
- p95 per-page processing latency <= 400ms.

## Failure Handling
- If decode fails, payload is preserved as passthrough artifact.
- If preprocessing fails, original decoded image is re-encoded and persisted.
- If request concurrency is saturated, service returns HTTP `503` so Temporal can retry.
- If preprocessing exceeds `PREPROCESS_REQUEST_TIMEOUT_SECONDS`, service returns HTTP `504`.
- Metadata records fallback reason to support debugging.

## Security and Compliance
- No filesystem writes for preprocessing artifacts in service runtime.
- Only normalized metadata is logged.

## Observability
- Service exposes HTTP metrics at `/metrics`.
- Error logs include job-level context for fallback paths.
- Response metadata includes per-request `duration_ms`.

## How To Run

Run with Docker Compose:
```bash
docker compose up -d preprocess-worker
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group preprocess
PYTHONPATH=. .venv/bin/uvicorn preprocess_worker.app.main:app --host 0.0.0.0 --port 8010 --reload
```

Run tests:
```bash
.venv/bin/python -m unittest discover -s preprocess_worker/tests -p 'test_*.py' -v
```

## Non-Goals
- OCR text recognition and field extraction.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations. No runtime code changes were applied to this service in this update.
- 2026-05-27: Implemented production-grade OpenCV preprocessing pipeline with robust skew estimation, configurable thresholding/denoise controls, safe fallback behavior, and unit tests.
- 2026-06-01: Added bounded worker-pool execution, lifespan shutdown, richer preprocessing metadata, configurable CLAHE/deskew/threshold/blur controls, retryable busy/timeout errors, and expanded unit tests.
- 2026-07-01: Corrected OpenCV rectangle-angle normalization, switched skew
  points to `(x, y)` coordinates, bounded deskew correction to 15 degrees by
  default, and added upright/skewed/implausible-angle regression tests.
