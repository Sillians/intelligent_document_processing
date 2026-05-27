# Intelligent Document Processing - Local Stack

This repository now contains a production-minded, on-premise **Intelligent Document Processing (IDP)** stack built with **Docker Compose** and a hybrid OCR pipeline:

- Baseline OCR: **PaddleOCR 3.x**
- Hard-page fallback: **vLLM OpenAI-compatible VLM inference**
- Confidence gating + HITL: automatic routing to **Label Studio**
- Workflow orchestration: **Temporal**
- API surface: **FastAPI**
- Preprocessing: **OpenCV**
- Layout pipeline: **LayoutParser** (uses Detectron2 when available)
- Experiment tracking: **MLflow**
- Monitoring: **Prometheus + Grafana**

## Why This Stack

For a single-host on-prem deployment with strong scale-up options and low operational friction:

1. `docker-compose` keeps local operations simple and reproducible.
2. `Temporal` gives reliable retries, idempotency, timeouts, and workflow state.
3. `SeaweedFS (S3-compatible) + Postgres` provides durable artifact + metadata storage on-prem.
4. `vLLM` is optional behind a `gpu` profile so CPU-only installs are not blocked.
5. Service boundaries map to horizontal scale points (`--scale preprocess-worker=3`, etc.).

## Topology

- `ingestion-service` (`FastAPI`): receives files and starts Temporal workflows.
- `workflow-orchestrator` (`Temporal Worker`): executes pipeline stages and branching.
- `preprocess-worker` (`OpenCV`): deskew/denoise/threshold.
- `ocr-service` (`PaddleOCR`): text + confidence extraction.
- `layout-service` (`LayoutParser` + Detectron2 when available): block segmentation.
- `classifier-router-service`: document type routing profile.
- `extraction-service` (`LangChain` + vLLM fallback): structured field extraction.
- `validation-service`: confidence + rule-based gating.
- `human-review-console`: creates HITL tasks in Label Studio.
- `delivery-service`: final artifact delivery.
- `evaluation-service` (`MLflow`): run metrics tracking.
- `temporal`, `temporal-ui`, `postgres`, `seaweedfs`, `mlflow`, `label-studio`, `prometheus`, `grafana`.




```
ingestion_service
workflow_orchestrator (basic state machine + queue publish/consume)
preprocess_worker + ocr_service (first functional pipeline)
layout_service
classifier_router_service + extraction_service
validation_service
delivery_service
evaluation_service + observability_stack
human_review_console
```

## Quick Start

1. Ensure Docker + Docker Compose plugin are installed.
2. Start core stack:

```bash
docker compose up -d --build
```

3. Start with VLM fallback on GPU host:

```bash
docker compose --profile gpu up -d --build
```

4. Submit a document:

```bash
curl -X POST "http://localhost:8000/documents" \
  -F "file=@/absolute/path/to/document.png"
```

5. Check workflow status:

```bash
curl "http://localhost:8000/documents/<job_id>"
```

## Run Completed Services Individually

The completed services at this stage are:
- `ingestion_service`
- `workflow_orchestrator`

Start required shared infrastructure first:

```bash
docker compose up -d postgres seaweedfs temporal temporal-ui
```

Run `ingestion_service` locally:

```bash
uv sync --frozen --no-default-groups --group temporal
PYTHONPATH=. .venv/bin/uvicorn ingestion_service.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Run `workflow_orchestrator` locally:

```bash
uv sync --frozen --no-default-groups --group orchestrator
PYTHONPATH=. .venv/bin/python -m workflow_orchestrator.app.worker
```

## Sample Document + E2E Test

A realistic invoice sample and end-to-end runner are included:

- Sample file: `samples/documents/sample_invoice_001.png`
- Generator: `scripts/generate_sample_invoice.py`
- E2E runner: `scripts/run_e2e.sh`

Run full e2e (upload, poll Temporal-backed status, fetch result, validate key fields):

```bash
./scripts/run_e2e.sh
```

Optional arguments and env:

```bash
# Use a custom sample path
./scripts/run_e2e.sh /absolute/path/to/your_document.png

# Tune timeout/polling/API endpoint
API_URL=http://localhost:8000 TIMEOUT_SECONDS=600 POLL_INTERVAL=5 ./scripts/run_e2e.sh

# Enforce strict required-field assertions even when routed to human review
STRICT_REQUIRED_FIELDS=1 ./scripts/run_e2e.sh
```

## Full Temporal Integration / E2E Against Live Stack

Use this for real end-to-end verification against running containers and Temporal state.

1. Prepare environment:

```bash
cp .env.example .env
```

2. Start the full live stack:

```bash
docker compose up -d --build
```

3. Verify critical services are healthy:

```bash
docker compose ps
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8088
```

4. Run e2e runtime test:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default ./scripts/run_e2e.sh
```

5. Expected success indicators:
- Script prints `E2E test completed successfully.`
- `workflow_final_status` in summary is `delivered` or `pending_human_review`.
- Temporal UI shows workflow execution in `COMPLETED` state.
- By default, missing required fields are only fatal when final status is `delivered`.
- Set `STRICT_REQUIRED_FIELDS=1` to fail e2e on any missing required fields.

6. Inspect workflow execution in Temporal UI:
- Open [http://localhost:8088](http://localhost:8088)
- Namespace: `default`
- Task queue: `idp-pipeline`

7. If test fails, check orchestrator and ingestion logs:

```bash
docker compose logs --tail=200 ingestion-service
docker compose logs --tail=200 workflow-orchestrator
docker compose logs --tail=200 preprocess-worker ocr-service layout-service extraction-service validation-service delivery-service
```

Common local failure case (SeaweedFS volume exhaustion):
- Symptom: e2e polling times out while `workflow-orchestrator` shows `extract_activity` timeouts and `extraction-service` shows S3 `PutObject InternalError`.
- Confirm:
```bash
curl -fsS http://localhost:9333/dir/status?pretty=y
```
- If `Topology.Free` is `0` and `extraction-artifacts` has no writable volumes, restart SeaweedFS with the current compose configuration (uses `-master.volumeSizeLimitMB=64` for more writable slots):
```bash
docker compose up -d seaweedfs
```
- If already exhausted with old persisted data, reset local data volumes and start clean:
```bash
docker compose down -v
docker compose up -d --build
```

## Service Endpoints

- Ingestion API: [http://localhost:8000](http://localhost:8000)
- Temporal UI: [http://localhost:8088](http://localhost:8088)
- Label Studio: [http://localhost:8080](http://localhost:8080)
- MLflow: [http://localhost:5001](http://localhost:5001) (or `http://localhost:${MLFLOW_HOST_PORT}`)
- Prometheus: [http://localhost:9090](http://localhost:9090)
- Grafana: [http://localhost:3000](http://localhost:3000)
- SeaweedFS S3 Endpoint: [http://localhost:8333](http://localhost:8333)
- SeaweedFS Master UI: [http://localhost:9333](http://localhost:9333)

## Scaling Guidance

Scale stateless workers independently based on bottlenecks:

```bash
docker compose up -d --scale preprocess-worker=3 --scale ocr-service=2 --scale extraction-service=2
```

Recommended progression:

1. Increase `preprocess-worker` and `ocr-service` for throughput.
2. Increase `workflow-orchestrator` workers for concurrency.
3. Enable `gpu` profile for `vllm` and heavy OCR/layout loads.
4. Move to K3s/Kubernetes when you need multi-node HA/autoscaling.

## Configuration

- Copy defaults from `.env.example` into `.env`.
- Sensitive production values should be injected by your secret manager.
- Main thresholds:
  - `OCR_CONFIDENCE_THRESHOLD`
  - `VLM_FALLBACK_THRESHOLD`
  - `AUTO_APPROVE_THRESHOLD`

## Dependency Management (uv)

This repository now uses **`pyproject.toml` + `uv.lock`** as the dependency source of truth.

- `requirements/` is removed.
- Service images install dependencies from lockfile-backed uv groups.
- Reproducible installs use `uv sync --frozen`.

Dependency groups are defined in [pyproject.toml](/Users/user/Projects/intelligent_document_processing/pyproject.toml):

- `base`
- `temporal`
- `orchestrator`
- `preprocess`
- `ocr`
- `layout`
- `extraction`
- `evaluation`
- `research`
- `dev`

Examples:

```bash
# Sync only OCR service dependencies
uv sync --frozen --no-default-groups --group ocr

# Sync only workflow orchestrator dependencies
uv sync --frozen --no-default-groups --group orchestrator

# Update lockfile after changing dependency groups
uv lock
```

## Observability

- Every API service exposes `/metrics` for Prometheus scraping.
- Grafana is pre-provisioned with an `IDP Overview` dashboard.
- Prometheus alert rules include service-down and elevated 5xx rate.

## Important Notes

- `layout-service` will use Detectron2-backed layout analysis when runtime dependencies are available; otherwise it falls back to a safe heuristic page block.
- `extraction-service` runs deterministic extraction first, then uses VLM fallback when confidence/route conditions are met.
- HITL tasks are sent to Label Studio when validation gates fail.
