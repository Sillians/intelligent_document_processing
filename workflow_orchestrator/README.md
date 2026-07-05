# Workflow Orchestrator

## Core Purpose
Execute the end-to-end IDP pipeline in Temporal, enforce stage contracts, apply branching decisions, and persist durable workflow state.

## Implemented Core Functionalities
1. Stage execution engine for ordered pipeline:
   - `preprocess_activity`
   - `ocr_activity`
   - `layout_activity`
   - `classify_activity`
   - `extract_activity`
   - `validate_activity`
2. Branching logic:
   - Human-review path via `create_review_task_activity`
   - Delivery path via `deliver_activity`
3. Evaluation finalization:
   - Best-effort `evaluate_activity` runs in workflow finalization for all outcomes.
4. Contract validation:
   - Strict payload/response contract checks between stages.
   - Contract violations fail as non-retryable `PipelineContractError`.
5. Retry and timeout policy:
   - Temporal retry policy on activities (`initial=2s`, `max=30s`, `attempts=3`).
   - Stage-specific timeouts (`5m` long stages, `30s` short stages).
6. Durable execution state:
   - Workflow history/checkpoints managed by Temporal.
7. Failure handling:
   - Retryable network/server failures.
   - Non-retryable downstream client/contract failures.
8. Worker runtime controls:
   - Configurable concurrency, cache, identity, and graceful shutdown.
9. Queue observability:
   - Prometheus endpoint on internal port `9091`.
   - Approximate Temporal workflow/activity backlog and poller gauges.

## Scope
- Orchestrate deterministic workflow execution.
- Coordinate cross-service pipeline calls through activities.
- Apply routing to delivery vs manual review.
- Provide reliable workflow completion semantics.

## Primary Inputs
- Workflow start payload from `ingestion_service`:
  - `job_id`
  - `raw_bucket`
  - `raw_key`
  - `tenant_id` (optional but supported)
  - `source` (optional but supported)

## Primary Outputs
- Workflow result object containing stage outputs and final status.
- Final statuses:
  - `delivered`
  - `pending_human_review`
  - `failed` (on workflow failure)

## Interfaces
- Temporal worker process (`python -m workflow_orchestrator.app.worker`).
- Activity HTTP integrations with:
  - `preprocess_worker`
  - `ocr_service`
  - `layout_service`
  - `classifier_router_service`
  - `extraction_service`
  - `validation_service`
  - `human_review_console`
  - `delivery_service`
  - `evaluation_service`

## How To Run

Prerequisites:
- Temporal server reachable at `TEMPORAL_ADDRESS`.
- Downstream stage services reachable at configured URLs.

Run with Docker Compose service:
```bash
docker compose up -d workflow-orchestrator
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group orchestrator
PYTHONPATH=. .venv/bin/python -m workflow_orchestrator.app.worker
```

Worker logs:
```bash
docker compose logs -f workflow-orchestrator
```

## Reliability and Error Semantics
- HTTP `4xx` (except `408`/`429`) are treated as non-retryable client errors.
- HTTP `5xx`, `408`, `429`, and network errors are retryable via Temporal activity retry policy.
- Invalid JSON/object response shapes are treated as non-retryable downstream contract errors.
- Evaluation tracking failures are logged and do not override branch outcome.
- Optional OCR network fallback can synthesize a minimal OCR artifact when OCR service is unavailable so workflow can continue to review/delivery path.

## Key Configuration
- `TEMPORAL_ADDRESS`
- `TEMPORAL_NAMESPACE`
- `TEMPORAL_TASK_QUEUE`
- `ORCHESTRATOR_HTTP_TIMEOUT_SECONDS`
- `ORCHESTRATOR_HTTP_CONNECT_TIMEOUT_SECONDS`
- `ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK`
- `TEMPORAL_WORKER_IDENTITY`
- `TEMPORAL_WORKER_MAX_CACHED_WORKFLOWS`
- `TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOW_TASKS`
- `TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES`
- `TEMPORAL_WORKER_GRACEFUL_SHUTDOWN_SECONDS`
- `WORKFLOW_METRICS_PORT` (default `9091`)
- `WORKFLOW_METRICS_INTERVAL_SECONDS` (default `15`)

## Observability
- Structured worker logs for startup, completion, and fatal failures.
- Stage failure context propagated through workflow/activity errors.
- Temporal UI exposes lifecycle and retries per workflow execution.
- `GET :9091/metrics` exposes `idp_temporal_task_queue_backlog`,
  `idp_temporal_task_queue_pollers`, and the last successful collection time.
- Backlog is Temporal's approximate `backlog_count_hint`, collected separately
  for workflow and activity tasks.

## Tests
Implemented unit tests for contract/branching logic and retry classification:
- [`workflow_orchestrator/tests/test_pipeline.py`](./tests/test_pipeline.py)
- [`workflow_orchestrator/tests/test_activities.py`](./tests/test_activities.py)
- [`workflow_orchestrator/tests/test_metrics.py`](./tests/test_metrics.py)

Run tests:
```bash
.venv/bin/python -m unittest discover -s workflow_orchestrator/tests -p 'test_*.py' -v
```

## Files Added/Updated in This Implementation
- [`workflow_orchestrator/app/workflows.py`](./app/workflows.py)
- [`workflow_orchestrator/app/activities.py`](./app/activities.py)
- [`workflow_orchestrator/app/worker.py`](./app/worker.py)
- [`workflow_orchestrator/app/pipeline.py`](./app/pipeline.py)
- [`workflow_orchestrator/app/metrics.py`](./app/metrics.py)
- [`workflow_orchestrator/tests/test_pipeline.py`](./tests/test_pipeline.py)
- [`workflow_orchestrator/tests/test_activities.py`](./tests/test_activities.py)

## Non-Goals
- Performing OCR/layout/extraction logic locally.
- Persisting business-domain outputs directly.

## Change Log
- 2026-05-21: Implemented production-grade Temporal orchestration with strict contracts, deterministic stage-payload builders, resilient activity HTTP error semantics, configurable worker concurrency/shutdown controls, and workflow-orchestrator unit tests.
- 2026-05-27: Added resilient OCR-network fallback path that persists deterministic OCR artifact when OCR service is unreachable or returns transient 5xx, controlled by `ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK`.
- 2026-07-03: Added Prometheus Temporal task-queue backlog, poller, and collection-freshness metrics on internal port `9091`.
