# Evaluation Service

## Core Purpose
Track production pipeline metrics, quality signals, and experiment lineage without allowing evaluation failures to block delivery or human-review outcomes.

**The service is both:**
- A production run tracker called by `workflow_orchestrator` after every pipeline outcome.
- An extensible evaluation boundary for later benchmark metrics such as CER, WER, precision, recall, F1, schema validity, and release gates.

## Implemented Core Functionalities
- Accepts production and benchmark metrics at `POST /track-run`.

- Tracks runs in MLflow through a modular provider.

- Persists metrics and receipts to S3-compatible object storage through an artifact provider.

- Supports multiple providers concurrently.

- Returns partial success when MLflow is unavailable but durable artifact tracking succeeds.

- Creates deterministic evaluation IDs for Temporal retry idempotency.

- Detects prior successful runs and returns their receipt without duplicating tracking.

- Derives standard pipeline quality and outcome metrics.

- Accepts arbitrary custom numeric metrics, parameters, tags, dataset versions, and pipeline versions.

- Persists evaluation receipts for auditability and status lookup.

- Bounds concurrent requests and retries transient provider failures.

- Exposes evaluation receipt lookup and provider health APIs.


## Provider Model
An evaluation provider implements:

```python
class EvaluationProvider(Protocol):
    name: str

    async def track(self, context: EvaluationContext) -> dict:
        ...
```

**Current providers:**
- `MLflowProvider`: tracks parameters, metrics, and tags in MLflow.
- `ArtifactStoreProvider`: persists normalized metrics and lineage to `EVALUATION_BUCKET`.

New providers can be registered in `evaluation_service/app/providers.py` for Prometheus remote write, OpenTelemetry, analytics warehouses, data-quality systems, custom scorecard stores, or release-gating systems.

## Primary Inputs
**Required production-run fields:**
- `job_id`
- `status`
- `ocr_confidence`
- `extraction_confidence`
- `used_vlm_fallback`
- `requires_human_review`

**Optional lineage and extensibility fields:**
- `route`
- `validation_verdict`
- `field_count`
- `populated_field_count`
- `custom_metrics`
- `parameters`
- `tags`
- `dataset_version`
- `pipeline_version`
- `idempotency_key`


**Example production request:**

```json
{
  "job_id": "job-123",
  "status": "delivered",
  "ocr_confidence": 0.94,
  "extraction_confidence": 0.91,
  "used_vlm_fallback": false,
  "requires_human_review": false,
  "route": "invoice",
  "validation_verdict": "auto_approved",
  "field_count": 5,
  "populated_field_count": 5,
  "pipeline_version": "release-1"
}
```

Example benchmark request with custom metrics:

```json
{
  "job_id": "benchmark-invoice-001",
  "status": "benchmark_completed",
  "ocr_confidence": 0.93,
  "extraction_confidence": 0.90,
  "used_vlm_fallback": false,
  "requires_human_review": false,
  "route": "invoice",
  "dataset_version": "invoice-gold-v2",
  "pipeline_version": "release-1",
  "custom_metrics": {
    "cer": 0.04,
    "wer": 0.08,
    "field_precision": 0.95,
    "field_recall": 0.92,
    "field_f1": 0.935
  },
  "parameters": {
    "ocr_model": "paddleocr",
    "extraction_profile": "invoice_v1"
  },
  "tags": {
    "benchmark_suite": "invoice-regression"
  }
}
```

The repository includes a dataset benchmark runner that can send aggregate metrics to this endpoint:

```bash
python3 scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 5 \
  --evaluation-url http://localhost:8018
```

For private gold datasets, use JSONL manifests documented in `data/README.md`.

## Derived Metrics
Every run derives these standard metrics:

- `ocr_confidence`
- `extraction_confidence`
- `confidence_mean`
- `confidence_gap`
- `used_vlm_fallback`: numeric `0` or `1`
- `requires_human_review`: numeric `0` or `1`
- `outcome_delivered`: numeric `0` or `1`
- `outcome_pending_human_review`: numeric `0` or `1`
- `outcome_failed`: numeric `0` or `1`
- `field_count` when provided
- `populated_field_count` when provided
- `field_completeness` when field counts are available

`custom_metrics` extends the metric set without requiring service code changes.

## Primary Outputs
- `job_id`
- `evaluation_id`
- `mlflow_run_id`
- `tracked`
- `tracking_status`: `success`, `partial_success`, or `failed`
- `provider_results`
- `evaluation_bucket`
- `evaluation_receipt_key`
- `idempotent_replay`

Example response:

```json
{
  "job_id": "job-123",
  "evaluation_id": "evaluation-abc123",
  "mlflow_run_id": "f3c...",
  "tracked": true,
  "tracking_status": "success",
  "provider_results": [
    {
      "provider": "mlflow",
      "status": "success",
      "run_id": "f3c...",
      "attempts": 1
    },
    {
      "provider": "artifact_store",
      "status": "success",
      "artifact": "s3://evaluation-artifacts/jobs/job-123/evaluation/evaluation-abc123/metrics.json",
      "attempts": 1
    }
  ],
  "evaluation_bucket": "evaluation-artifacts",
  "evaluation_receipt_key": "jobs/job-123/evaluation/evaluation-abc123/receipt.json",
  "idempotent_replay": false
}
```

## Interfaces
- `GET /health`
- `POST /track-run`
- `GET /evaluations/{job_id}/{evaluation_id}`
- Evaluation API docs: `http://localhost:8018/docs`
- MLflow UI: `http://localhost:5001`

## MLflow Provider
Default MLflow configuration:

```env
MLFLOW_TRACKING_URI=http://mlflow:5000
EVALUATION_MLFLOW_EXPERIMENT=idp_pipeline
```

The MLflow provider logs:
- Standard and custom numeric metrics.
- Pipeline status, route, validation verdict, dataset version, and pipeline version as parameters.
- Evaluation ID and custom tags.

Before creating a run, the provider searches the experiment for the deterministic `evaluation_id` tag. This prevents duplicate MLflow runs when Temporal retries after an incomplete response.

Open the MLflow UI at:

```text
http://localhost:5001
```


## Artifact Store Provider

**Default artifact configuration:**

```env
EVALUATION_BUCKET=evaluation-artifacts
EVALUATION_PROVIDERS=mlflow,artifact_store
```

**Metrics artifact pattern:**

```text
jobs/<job_id>/evaluation/<evaluation_id>/metrics.json
```

**Receipt artifact pattern:**

```text
jobs/<job_id>/evaluation/<evaluation_id>/receipt.json
```

If evaluation receipt persistence fails in `EVALUATION_BUCKET`, the service falls back to `DELIVERY_BUCKET`.

## Failure Isolation
Evaluation runs in the workflow `finally` block and must not change a delivery or review outcome.

**Default behavior:**

```env
EVALUATION_FAIL_WHEN_ALL_PROVIDERS_FAIL=false
```

When MLflow fails but artifact storage succeeds, the API returns:

```json
{
  "tracked": true,
  "tracking_status": "partial_success"
}
```

When every provider fails, the service persists the failed receipt where possible and returns `tracked=false` unless strict failure mode is enabled. The workflow also catches evaluation activity failures and logs them without changing the final document status.

## Idempotency
When `idempotency_key` is provided, it controls the deterministic evaluation ID. Otherwise, the service derives the evaluation ID from the normalized request.

A prior successful or partially successful receipt is returned with:

```json
{
  "idempotent_replay": true
}
```

## Key Configuration
- `EVALUATION_BUCKET`
- `EVALUATION_PROVIDERS`
- `EVALUATION_MLFLOW_EXPERIMENT`
- `EVALUATION_DATASET_VERSION`
- `EVALUATION_PIPELINE_VERSION`
- `EVALUATION_REQUEST_TIMEOUT_SECONDS`
- `EVALUATION_MAX_INFLIGHT_REQUESTS`
- `EVALUATION_RETRY_MAX`
- `EVALUATION_RETRY_BACKOFF_SECONDS`
- `EVALUATION_FAIL_WHEN_ALL_PROVIDERS_FAIL`
- `MLFLOW_TRACKING_URI`
- `MLFLOW_BUCKET`

## Failure Handling
- Unknown providers return HTTP `422`.
- Busy evaluation capacity returns HTTP `503` so Temporal can retry.
- Request timeout returns HTTP `504`.
- Provider failures are recorded independently.
- Partial provider success remains a successful evaluation outcome.
- Evaluation receipts preserve provider error details and attempt counts.

## Security and Compliance
- Dataset and pipeline versions preserve metric lineage.
- Evaluation artifacts provide a durable audit record outside MLflow.
- Custom parameters and tags should avoid raw sensitive document contents.
- Credentials and tracking URLs should be injected through environment variables or a secret manager.

## Observability
- FastAPI app is instrumented with the shared Prometheus middleware.
- Evaluation receipts contain provider attempts, duration, tracking status, metrics, parameters, tags, and errors.
- Useful operational metrics include tracking success rate, MLflow availability, partial-success rate, evaluation latency, confidence trends, human-review rate, and VLM fallback rate.

## How To Run

Start the evaluation service and MLflow:

```bash
docker compose up -d mlflow evaluation-service
```

Open MLflow:

```text
http://localhost:5001
```

Run locally from repository root:

```bash
uv sync --frozen --no-default-groups --group evaluation
PYTHONPATH=. .venv/bin/uvicorn evaluation_service.app.main:app --host 0.0.0.0 --port 8018 --reload
```

Run tests:

```bash
.venv/bin/python -m unittest discover -s evaluation_service/tests -p 'test_*.py' -v
```

Run focused workflow contract tests:

```bash
.venv/bin/python -m unittest workflow_orchestrator.tests.test_pipeline -v
```

## Next Evaluation Extensions
- CER and WER calculation from OCR ground truth.
- Route-specific field precision, recall, and F1.
- Schema-validity and business-rule accuracy metrics.
- Regression comparison against a baseline pipeline version.
- Release gates and automated regression alerts.

## Non-Goals
- Blocking live delivery based on evaluation-provider availability.
- Replacing the validation service's per-document approval gate.
- Storing raw sensitive document contents in MLflow parameters or tags.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations.
- 2026-06-04: Implemented modular MLflow and artifact-store providers, normalized and custom metrics, metric lineage, idempotency, provider fallback, durable receipts, lookup APIs, tests, and workflow integration.
