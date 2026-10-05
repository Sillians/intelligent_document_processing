# Workflow Orchestrator

## Independent Page Execution (2026-10-03)
New workflows use patch `independent-pages-v1`. After bounded document rendering,
each page has separate `ocr_page_activity` and `layout_page_activity` commands,
stable IDs (`ocr-page-0001`, `layout-page-0001`, etc.), and its own retry history.
A page-two failure/restart reuses completed page-one results. Sequential admission
bounds CPU and memory; this is independent durable execution, not parallel fanout.
Rendering/preprocessing remains a bounded document activity and is not yet
independently checkpointed per page. Exhausted OCR failures retain page number in
the durable exception. No partial document proceeds to extraction or delivery.

`aggregate_pages_activity` validates ordered/unique manifests, expected scoped
keys/buckets, tenant/job/page identity, schema version and source-image identity.
It remaps page-local tokens/zones into ordered document coordinates and unique
block IDs, preserving source keys, dimensions and engine provenance. Canonical
`ocr/ocr.json` and `layout/layout.json` are written only after all pages validate.
Storage writes are idempotent, not a cross-bucket transaction; an upload failure
retries aggregation and prevents downstream execution. Large tokens/blocks stay
in artifacts rather than activity results. Per-page native-text references use
PDF coordinates separately from OCR's preprocessed-pixel coordinates.

Old histories retain the document-wide branch when the marker is absent.
The complete manifest is checked before any page activity is scheduled; duplicate
source keys and foreign-tenant references cannot partially dispatch a document.
Register all three new activities before deploying workflows. Keep the marker
until older histories retire. Use the existing run commands below.

```bash
.venv/bin/python -m unittest tests.test_page_execution -v
docker --context idp-staging compose -f infra/verification/compose.yml exec -T ingestion-service \
  python -m scripts.verify_page_execution
docker --context idp-staging compose -f infra/verification/compose.yml exec -T ingestion-service \
  python -m scripts.replay_pipeline_histories artifacts/live-milestone artifacts/recovery-delivery artifacts/page-execution \
  --report artifacts/page-execution/replay.json
```

The live drill deliberately fails page two once, restarts a worker, then executes
real OCR/layout, Label Studio correction, revalidation and delivery. It records
attempt counts and ordered page artifacts. It is not a SIGKILL drill or quality
benchmark; inspect the report's actual pass flag before claiming verification.
See `infra/quality/README.md` for representative evidence requirements.

## Durable Delivery Continuation (2026-10-01)

New workflows use the deterministic `durable-delivery-v1` patch. Submission
registers delivery work, then `poll_delivery_activity` reads the tenant/job-scoped
ledger receipt. HTTP 202/pending/dead-letter is not success. The queryable
business status is `pending_delivery` while work retries and `delivery_exception`
when an operator must redrive it. The workflow reports `delivered` only after
all destinations and receipt projection are acknowledged. An authorized redrive
can therefore resume the same waiting workflow without creating another delivery.

`DELIVERY_WAIT_TIMEOUT_SECONDS=43200` bounds this wait to 12 hours by default;
`DELIVERY_POLL_INTERVAL_SECONDS=10` controls durable Temporal timers. Ingestion
passes these values into workflow input; direct callers may supply the input
values. Exhaustion returns `delivery_timed_out`, never a false delivered status.
The queue can still complete after that deadline; reconcile late effects before
retrying or redriving. The deadline does not cancel external side effects.
Overall workflow execution timeout must cover review plus delivery wait.

Retryable delivery activity errors preserve the workflow's bounded wait;
nonretryable activity errors still fail explicitly. Polling checks response
tenant/job identity. Register the new polling activity with the worker before
starting patched workflows. The false patch branch retains historical command
ordering; do not remove the marker until older histories have retired. Both
new histories and earlier pre-patch histories replayed cleanly together (15 total)
using `scripts/replay_pipeline_histories.py`; see the verification README for the
batch command and an observed ad hoc replay teardown failure.

Replacement processing inputs carry `reprocess_parent_job_id` and
`reprocess_generation`. These are included in delivery metadata, not used to
change the failed parent's workflow ID or result. Reprocessing uses the standard
OCR/review/validation path, including no-delivery rejection and timeout outcomes.

Run this worker using the existing commands below. Functional verification:

```bash
.venv/bin/python -m unittest workflow_orchestrator.tests.test_workflows -v
bash scripts/run_recovery_delivery_verification.sh
```

The live gate verifies reprocessed Label Studio decisions through acknowledged
delivery and replays the two new histories against this production workflow.
Independent per-page activity execution and representative gold-set acceptance
remain the next milestone; document fixtures cannot substitute for those gates.

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
  - `tenant_id` (required for tenant isolation; default tenant retained for legacy inputs)
  - `source` (optional but supported)

## Primary Outputs
- Workflow result object containing stage outputs and final status.
- Final statuses:
  - `delivered`
  - `rejected`, `review_validation_failed`, or `review_timed_out`
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
- OCR network failures remain retryable activity errors; no synthetic OCR artifact is created.

## Key Configuration
- `TEMPORAL_ADDRESS`
- `TEMPORAL_NAMESPACE`
- `TEMPORAL_TASK_QUEUE`
- `ORCHESTRATOR_HTTP_TIMEOUT_SECONDS`
- `ORCHESTRATOR_HTTP_CONNECT_TIMEOUT_SECONDS`
- `ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK` (legacy ignored setting; keep false)
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

## 2026-09-30: Review Continuation And Explicit OCR Failure

The workflow now waits for a durable review decision instead of ending when it creates a review task. It polls the review service at `REVIEW_POLL_INTERVAL_SECONDS` (default 60), for up to `REVIEW_WAIT_TIMEOUT_SECONDS` (default 604800). An approval merges corrected fields, runs validation with `human_review_approved=true`, then delivers with reviewer and decision provenance. Rejection, failed correction validation, and timeout are terminal outcomes without delivery. The public API can query the pending review snapshot.

OCR transport failures now follow Temporal retries and fail explicitly after retries. `ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK` is retained only for old configuration compatibility and no longer creates a synthetic OCR artifact. Stage requests carry `tenant_id` and `X-IDP-Internal-Key` when configured. The review polling activity uses `REVIEW_DECISION_API_KEY`; configure it before routing documents to HITL.
# Page-Aware Orchestration (2026-09-30)

The preprocess response's ordered `pages` manifest is forwarded to OCR and layout. Their aggregate artifacts still satisfy the existing downstream classifier/extractor payloads, while preserving page numbers in artifacts. Benchmark the page-aware path with both single- and multi-page files before considering any service consolidation.
The ingestion start payload carries page-aware stage timeout budgets. Keep preprocess/OCR/layout service timeouts and `ORCHESTRATOR_HTTP_TIMEOUT_SECONDS` aligned across deployments; multi-page activity deadlines scale with page count.

### Review and OCR regression verification

Run the focused unit/contract suite from the repository root:

```bash
.venv/bin/python -m unittest workflow_orchestrator.tests.test_workflows \
  human_review_console.tests.test_main ocr_service.tests.test_main \
  workflow_orchestrator.tests.test_activities tests.test_ocr_failure \
  delivery_service.tests.test_main validation_service.tests.test_main -v
```

Workflow tests verify corrected fields reach revalidation and delivery, while
rejection, invalid corrections and review timeout never invoke delivery. These
use mocked workflow activities/time; they do not exercise a live Temporal server,
Label Studio instance, durable DB replay, or external delivery receiver. Synthetic
benchmark results are neither substitutes for these tests nor quality evidence.
Typed OCR failures now retain their retryability across the HTTP/activity boundary.

### Persistent OCR exception routing

New histories use Temporal patch `durable-ocr-exception-v1`. Typed terminal OCR
failures and exhausted retryable failures execute `record_ocr_exception_activity`
with tenant/job/code, and finish with business status `ocr_exception` only after
Postgres persistence succeeds. Delivery is not invoked. Network/legacy server
errors are normalized to retryable `OCR_UNAVAILABLE`. Old histories without the
patch preserve their existing error path. Worker registration includes the new
activity. Regression tests cover retry-exhaustion routing and suppression of
delivery. Live worker recovery and exported-history replay have also passed;
see the completed verification section below.

### Completed live worker recovery and history replay

The isolated `idp-live-verification` Compose project now runs on `idp-staging`.
The live drill kills the worker with SIGKILL while a real Label Studio review is
pending, starts a new worker process, approves corrected values, and checks the
resulting delivery artifact and reviewer identity. Approval, rejection, invalid
correction, timeout, and exhausted OCR retry histories are exported and replayed
with Temporal SDK `Replayer(workflows=[DocumentPipelineWorkflow])`.

Run `bash scripts/run_live_milestone.sh` from the repository root. See
`infra/verification/README.md` for endpoints, local credentials, and evidence.
This verifies recovery at the review-wait boundary and deterministic replay of
these new histories. It does not retroactively prove compatibility with every
workflow history created before these changes.

## 2026-10-01: Recoverable Intake Contract

Ingestion now claims its dispatch outbox with an opaque lease token and requires
that token to acknowledge the workflow. Recovery after a successful Temporal
start reuses the tenant/job workflow ID and preserves the existing run ID; it
does not create a replacement processing generation. The live intake gate checks
five process-crash windows, fenced acknowledgements, and transactional audits
using this worker's real Temporal server. Run
`bash scripts/run_ingestion_verification.sh`; see the ingestion service README.
Explicit exception resolution/new-generation reprocessing is verified by the
separate recovery/delivery gate above; it never reuses the failed workflow's ID.
