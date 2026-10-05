# OCR Service

## Independent Page Requests (2026-10-03)
`POST /ocr` accepts optional `page_slot` (1..1000). A slotted request must contain
one image page; local page numbering remains one inside its stage artifact, while
the slot records original document identity. Artifacts use
`ocr/pages/0001.json`, etc., instead of overwriting the document-wide key.
The artifact includes tenant/job/slot identity, real engine configuration,
secondary-engine provenance and source image. The Temporal aggregator validates
and remaps these to document page numbers. Unslotted clients retain the previous
document API. Synthetic OCR success remains disabled; empty/outage outcomes are
typed failures and page-local retries do not rerun completed pages.

Run with the existing commands below. Unit gate:
`.venv/bin/python -m unittest ocr_service.tests.test_main tests.test_page_execution -v`.
Live retry/aggregation command and evidence are in `infra/verification/README.md`.
The verification profile uses real CPU Tesseract, not production-quality gold labels.

## Core Purpose
Convert processed document images into machine-readable text with coordinates
and confidence using a configurable OCR backend.

## Scope
- Load PaddleOCR with version-compatible init/inference paths for the full
  production profile.
- Run Tesseract for the compact, CPU-only staging profile.
- Extract token text, bounding geometry, and confidence scores.
- Emit `full_text`, token count, and mean confidence for downstream stages.
- Return an explicit failure when OCR cannot produce real text.
- Run blocking OCR workload in a bounded worker pool to avoid blocking FastAPI event loop.
- Guard request execution with timeout and return 503 when capacity is exhausted.

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
- If worker slots are saturated, the service returns 503 so Temporal can retry.
- When `OCR_FORCE_FALLBACK=true` or `OCR_BACKEND=fallback`, readiness and OCR requests fail with 503.
- An engine failure or timeout returns 503; an invalid image or empty recognition result returns 422.

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

## 2026-09-30: No Synthetic OCR Success

`POST /ocr` validates the tenant/job scope of `preprocessed_key`. Engine failures, request timeouts, and capacity exhaustion return non-2xx responses; invalid images or pages with no recognized text return 422. No fallback token is persisted. `OCR_FORCE_FALLBACK=true` / `OCR_BACKEND=fallback` makes readiness and requests return 503. Select a real CPU backend such as `OCR_BACKEND=tesseract` for staging, or a tested Paddle backend on suitable hardware. Non-default tenant OCR artifacts use `tenants/<tenant_id>/jobs/<job_id>/ocr/...`. Internal service authentication is controlled by `IDP_REQUIRE_INTERNAL_AUTH` and `IDP_INTERNAL_API_KEY`.
# Page-Aware Contract (2026-09-30)

`POST /ocr` accepts `preprocessed_pages` from preprocessing, validates every tenant-scoped key and sequential page number, and fails the request if any page has an engine/read error. Its artifact has ordered `pages` with page-local tokens, page text and confidence; flattened document `tokens` carry `page_number`, and `full_text` covers all pages. `page_count` and `schema_version=2.0` are returned. The original `preprocessed_key` request field remains the page-one alias for compatibility. A blank page is allowed if another page contains text; an all-blank document fails with 422.
`OCR_REQUEST_TIMEOUT_SECONDS` is a per-page budget multiplied by the manifest page count; the orchestrator HTTP and Temporal deadlines are set above this service budget.

### Typed OCR failures

Processing errors use HTTP `detail` objects with `schema_version: "1.0"`,
`stage: "ocr"`, `code`, and `retryable`. Codes include `OCR_INVALID_IMAGE`,
`OCR_NO_TEXT`, `OCR_TIMEOUT`, `OCR_BUSY`, `OCR_DISABLED`, and `OCR_UNAVAILABLE`.
Invalid images (including page-prefixed errors), no text, and disabled OCR are
nonretryable; transient engine failures, timeouts and saturation are retryable.
Orchestrator activities preserve the error type and retryability in Temporal
ApplicationError. Error responses never substitute synthetic document text.
Health/readiness errors remain operational responses, not stage-result contracts.

Tests cover page-prefixed classification and absence of synthetic persistence.
Retry exhaustion now routes to a persisted tenant exception, as exercised by
the live outage drill documented below.

### Validated secondary engine

Set `OCR_SECONDARY_BACKEND=tesseract` and optionally
`OCR_SECONDARY_MIN_CONFIDENCE=0.80` to permit actual Tesseract output following
primary engine failure. Default `none` disables secondary execution. Tesseract
must be installed in the OCR runtime. It is not attempted for invalid images,
artifact download failures, forced fallback, or when Tesseract is already primary.
Every token must have text, finite confidence in [0,1], and a nondegenerate
four-point box inside the image; average confidence must meet the configured
threshold. Rejected secondary output becomes `OCR_SECONDARY_REJECTED` and never
becomes document text. Page artifacts record secondary engine configuration and
version, and document `fallback_used` records secondary use. Primary-empty output
is still `OCR_NO_TEXT`; blank pages are permitted in otherwise readable documents.

Unit tests use controlled engine output; the live drill below also executes
real Tesseract. Representative production accuracy remains a separate gate.

### Completed real OCR verification

Homebrew Tesseract 5.5.3 is installed on the host; the CPU verification image
contains Tesseract 5.5.0. Live calls verified a real primary Tesseract pipeline,
real secondary Tesseract execution when Paddle is unavailable, rejection below
the configured confidence threshold, and persistence of accepted text and engine
version. Secondary pages record `primary_failure`; document `engine_backend`
reports `tesseract` or `mixed` accurately. Readiness can report `degraded` with a
working secondary engine when primary initialization failed.

A live primary service outage exhausted three Temporal activity attempts and
persisted `OCR_UNAVAILABLE` for the correct tenant. See
`infra/verification/README.md` and `artifacts/live-milestone/report.json` for the
repeatable drill and measured functional outcomes. These fixture outcomes do not
approve OCR accuracy for a representative production corpus.

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
