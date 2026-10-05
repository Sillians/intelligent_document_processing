# Preprocess Worker

## Page Admission And Native Text (2026-10-03)
`PREPROCESS_MAX_TOTAL_PIXELS=50000000` adds a document-wide decoded-pixel budget
alongside page count, maximum dimension and the 25-megapixel per-raster-page cap.
PDF dimensions are checked before rendering; TIFF frames before conversion;
single-image headers before OpenCV decode. Corrupt/unsafe decode failures return
422; dependency and memory failures are not disguised as invalid content.
This reduces decompression risk but is not a complete RSS sandbox. Enforce upload
size, container memory/CPU limits and request admission in deployment as well.

PDF native text is preserved separately as `preprocessed/native-0001.json`, etc.,
with tenant/job/page, PDF-point coordinate provenance and a 100,000-character
limit and an explicit `truncated` flag. Page manifests carry the key, not large
text bodies. OCR still processes
the rendered pixels; native text is not silently assigned perfect confidence or
used to conceal an OCR outage. Native-text preference/coordinate evidence needs
an independently reviewed quality policy before it replaces real OCR.

Rendering and preprocessing remain one bounded document activity. OCR and layout
now execute independently per page through Temporal, so their retries do not
rerender successful preprocessing. Rendering itself has no per-page checkpoint.
Use the existing run commands below and
`.venv/bin/python -m unittest preprocess_worker.tests.test_main -v` for regression.

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

## 2026-09-30: Tenant Scope

`POST /preprocess` accepts `tenant_id` (default `default`) and checks that `raw_key` belongs to that tenant and job before reading it. Non-default tenant outputs use `tenants/<tenant_id>/jobs/<job_id>/preprocessed/...`. Configure `IDP_REQUIRE_INTERNAL_AUTH=true` and `IDP_INTERNAL_API_KEY` in staging/production; the orchestrator sends `X-IDP-Internal-Key`. The default tenant's existing `jobs/<job_id>/...` keys remain supported.
# Page-Aware Contract (2026-09-30)

The worker now renders every PDF page with PDFium, decodes every TIFF frame with Pillow, and handles ordinary images as one-page documents. `PREPROCESS_MAX_PAGES` defaults to 25 and `PREPROCESS_PDF_DPI` to 150; oversized page counts and unreadable documents fail explicitly. PDF render size is bounded by `PREPROCESS_MAX_DIMENSION`. Every page becomes `preprocessed/page-NNNN.png` and the response contains `schema_version=2.0`, `page_count`, and ordered `pages` with `page_number`, `preprocessed_key`, dimensions, and preprocessing metadata. The old `preprocessed_key` remains an alias for page one, not a document-wide image. OCR must use the `pages` manifest rather than that alias for multi-page input.
Raster source pages above 25 megapixels are rejected before preprocessing; raise this limit only after a memory-budget review.
`PREPROCESS_REQUEST_TIMEOUT_SECONDS` is treated as a per-page budget for the document endpoint. Keep the same configuration in ingestion and orchestration so their Temporal deadline exceeds the service deadline.

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
