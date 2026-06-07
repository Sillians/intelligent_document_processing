# Extraction Service

## Core Purpose
Transform OCR and layout artifacts into structured business fields, confidence signals, and validation-ready payloads.

## Implemented Core Functionalities
- Reads OCR artifacts from `OCR_BUCKET`.
- Reads layout artifacts from `LAYOUT_BUCKET`.
- Prefers layout-zone text when it is complete enough; otherwise falls back to OCR token text.
- Runs deterministic route-aware extraction first.
- Supports schemas for:
  - `invoice`
  - `receipt`
  - `contract`
  - `purchase_order`
  - `bank_statement`
  - `generic`
- Uses deterministic extraction by default for efficient CPU/Apple Silicon development.
- Can optionally use LangChain + an OpenAI-compatible LLM/VLM fallback when confidence is low, route is `generic`, or OCR fallback text is detected.
- Produces field-level confidence estimates, missing-field lists, fallback metadata, and source metadata.
- Persists extraction output and returns the workflow-contract response fields.
- Runs blocking artifact reads in a bounded worker pool.

## Primary Inputs
- `job_id`
- `ocr_key`
- `layout_key`
- `ocr_confidence`
- `route`
- `strategy_profile` optional
- `extraction_mode` optional

## Primary Outputs
- `extraction_bucket`
- `extraction_key`
- `fields`
- `confidence`
- `used_vlm_fallback`

## Interfaces
- `GET /health`
- `POST /extract`

Example request:
```json
{
  "job_id": "job-123",
  "ocr_key": "jobs/job-123/ocr/ocr.json",
  "layout_key": "jobs/job-123/layout/layout.json",
  "ocr_confidence": 0.91,
  "route": "invoice",
  "strategy_profile": "invoice_v1",
  "extraction_mode": "deterministic_then_vlm"
}
```

## Key Configuration
- `EXTRACTION_BUCKET`
- `LAYOUT_BUCKET`
- `EXTRACTION_REQUEST_TIMEOUT_SECONDS`
- `EXTRACTION_MAX_INFLIGHT_REQUESTS`
- `EXTRACTION_ENABLE_VLM_FALLBACK`
- `EXTRACTION_VLM_TIMEOUT_SECONDS`
- `EXTRACTION_PROMPT_MAX_CHARS`
- `VLM_FALLBACK_THRESHOLD`
- `VLLM_BASE_URL`
- `VLLM_MODEL`
- `VLLM_API_KEY`

## Data and Storage
- Primary artifact bucket: `EXTRACTION_BUCKET`.
- Fallback artifact bucket, if primary write fails: `LAYOUT_BUCKET`.
- Artifact key pattern:
```text
jobs/<job_id>/extraction/result.json
```

## Failure Handling
- Partial extraction is returned when some fields are missing.
- Low-confidence/generic/OCR-fallback documents attempt VLM fallback when enabled.
- VLM failure does not fail deterministic extraction.
- Primary artifact upload falls back to `LAYOUT_BUCKET`.
- Busy worker pool returns HTTP `503` so Temporal can retry.
- Request timeout returns HTTP `504`.

## How To Run

Run with Docker Compose service:
```bash
docker compose up -d extraction-service
```

Apple Silicon/MPS local default:
```env
EXTRACTION_ENABLE_VLM_FALLBACK=false
```

This keeps extraction deterministic and avoids starting the Docker `vllm` service, which is intended for GPU-backed hosts through the Compose `gpu` profile.

Optional local OpenAI-compatible fallback with Ollama:
```env
EXTRACTION_ENABLE_VLM_FALLBACK=true
VLLM_BASE_URL=http://host.docker.internal:11434
VLLM_API_KEY=ollama
VLLM_MODEL=<your-ollama-model-name>
```

Optional local OpenAI-compatible fallback with LM Studio:
```env
EXTRACTION_ENABLE_VLM_FALLBACK=true
VLLM_BASE_URL=http://host.docker.internal:1234
VLLM_API_KEY=lm-studio
VLLM_MODEL=<your-loaded-model-name>
```

Optional GPU-backed vLLM service:
```bash
docker compose --profile gpu up -d vllm
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group extraction
PYTHONPATH=. .venv/bin/uvicorn extraction_service.app.main:app --host 0.0.0.0 --port 8014 --reload
```

Run tests:
```bash
.venv/bin/python -m unittest discover -s extraction_service/tests -p 'test_*.py' -v
```

## Non-Goals
- Final business approval.
- Human-review task creation.
- Outbound delivery.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations.
- 2026-05-26: Added resilient artifact persistence fallback from `EXTRACTION_BUCKET` to `LAYOUT_BUCKET`.
- 2026-06-02: Implemented route-aware structured extraction, layout text integration, bounded worker execution, LangChain/vLLM fallback controls, richer confidence metadata, and unit tests.
- 2026-06-02: Changed local default to deterministic extraction for Apple Silicon/MPS development and documented optional Ollama, LM Studio, and GPU vLLM fallback endpoints.
