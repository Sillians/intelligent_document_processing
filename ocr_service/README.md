# OCR Service

## Core Purpose
Convert processed document images into machine-readable text with coordinates
and confidence using a configurable OCR backend.

## Scope
- Load PaddleOCR with version-compatible init/inference paths for the full
  production profile.
- Run Tesseract for the compact, CPU-only staging profile.
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
- `engine_backend`

## Interfaces
- `GET /health`
- `GET /ready` (fails when the configured real OCR engine did not initialize)
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
- `OCR_BACKEND`: `paddle`, `tesseract`, or `fallback`
- `OCR_DISABLE_MKLDNN`
- `OCR_ENGINE_TIMEOUT_SECONDS`
- `OCR_ENGINE_LOCK_TIMEOUT_SECONDS`
- `OCR_REQUEST_TIMEOUT_SECONDS`
- `OCR_MAX_INFLIGHT_REQUESTS`
- `OCR_FORCE_FALLBACK`
- `OCR_TESSERACT_OEM`
- `OCR_TESSERACT_PSM`

## SLI / SLO Targets
- OCR service availability >= 99.5%.
- p95 per-page OCR latency <= 1.2s (engine-dependent).

## Failure Handling
- OCR engine initialization/runtime errors are caught and logged.
- OCR request execution runs in an isolated worker pool with timeout guard to avoid hanging workflow stages.
- If worker slots are saturated, service returns deterministic fallback immediately (`service_busy`) instead of queueing indefinitely.
- When `OCR_FORCE_FALLBACK=true` or `OCR_BACKEND=fallback`, OCR service skips
  the runtime and emits a deterministic fallback token.
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

For compact CPU OCR, install Tesseract on the host and use the `ocr-cpu` group:

```bash
# macOS
brew install tesseract

uv sync --frozen --no-default-groups --group ocr-cpu
OCR_BACKEND=tesseract OCR_FORCE_FALLBACK=false \
  PYTHONPATH=. .venv/bin/uvicorn ocr_service.app.main:app --host 0.0.0.0 --port 8011
```

Confirm the active backend:

```bash
curl -fsS http://localhost:8011/health
curl -fsS http://localhost:8011/ready
```

The staging release image installs `tesseract-ocr` and `pytesseract`, is
published for both `linux/amd64` and `linux/arm64`, and defaults to:

```env
OCR_BACKEND=tesseract
OCR_FORCE_FALLBACK=false
OCR_TESSERACT_OEM=1
OCR_TESSERACT_PSM=3
```

Paddle remains the production default. Tesseract staging establishes functional
accuracy on CPU; benchmark both engines before promoting an OCR backend change.
CI builds the CPU image and requires Tesseract to recognize `INVOICE` in the
sample document before release-candidate publication can proceed.

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
- 2026-07-01: Added the Tesseract CPU backend, backend-aware health and artifact
  metadata, an `ocr-cpu` dependency group, and native amd64/arm64 staging image
  publication while preserving Paddle as the production default.
