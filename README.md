# Intelligent Document Processing - Local Stack

This repository now contains a production-minded, on-premise **Intelligent Document Processing (IDP)** stack built with **Docker Compose** and a hybrid OCR pipeline:

- Baseline OCR: **PaddleOCR 3.x**, with compact **Tesseract CPU OCR** for staging
- Hard-page fallback: optional **OpenAI-compatible LLM/VLM inference** via local Ollama/LM Studio or GPU-backed vLLM
- Confidence gating + HITL: automatic routing to **Label Studio**
- Workflow orchestration: **Temporal**
- API surface: **FastAPI**
- API gateway / local load balancer: **Traefik**
- Preprocessing: **OpenCV**
- Layout pipeline: **LayoutParser** (uses Detectron2 when available)
- Experiment tracking: **MLflow**
- Monitoring: **Prometheus + Grafana**

## Production Operations

The current production baseline is still Docker Compose, hardened before any Minikube or Kubernetes migration. Use:

- `.env.production.example` for production configuration shape.
- `docker-compose.prod.yml` as the production override.
- `docker-compose.staging.yml` as the staging image override for release-candidate deploys.
- `docker-compose.release.yml` as the production image override for approved
  SHA-tagged release-candidate deploys.
- `scripts/production_preflight.py` to reject unsafe secrets and exposure settings.
- `infra/production/README.md` for the security, deployment, scaling, backup, restore, and rollback runbook.
- `infra/staging/OPERATIONAL_READINESS.md` for staging smoke, backup/restore, rollback, and alert drills.
- `infra/api/PUBLIC_API.md` for the public client/API consumer contract.
- `infra/ci/README.md` for the CI/CD milestone and GitHub Actions checks.

Start production only after replacing all placeholders:

```bash
python3 scripts/production_preflight.py --env-file .env.production
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## Why This Stack

For a single-host on-prem deployment with strong scale-up options and low operational friction:

1. `docker-compose` keeps local operations simple and reproducible.
2. `Temporal` gives reliable retries, idempotency, timeouts, and workflow state.
3. `SeaweedFS (S3-compatible) + Postgres` provides durable artifact + metadata storage on-prem.
4. `vLLM` is optional behind a `gpu` profile so CPU-only and Apple Silicon installs are not blocked.
5. Service boundaries map to horizontal scale points (`--scale preprocess-worker=3`, etc.).

## Topology

- `ingestion-service` (`FastAPI`): receives files and starts Temporal workflows.
- `gateway` (`Traefik`): public API edge for `/`, `/documents*`, and `/health`.
- `workflow-orchestrator` (`Temporal Worker`): executes pipeline stages and branching.
- `preprocess-worker` (`OpenCV`): deskew/denoise/threshold.
- `ocr-service` (`PaddleOCR` or `Tesseract`): text + confidence extraction.
- `layout-service` (`LayoutParser` + Detectron2 when available): block segmentation.
  - OCR Integration: Seamlessly pairs with Optical Character Recognition (OCR) engines like Tesseract, Google Cloud Vision, or open-source local models to extract clean text from specific layout zones.
- `classifier-router-service`: document type routing profile.
- `extraction-service` (`LangChain` + optional OpenAI-compatible fallback): structured field extraction.
- `validation-service`: confidence + rule-based gating.
- `human-review-console`: creates HITL tasks in Label Studio.
- `delivery-service`: final artifact delivery and terminal webhook event delivery.
- `evaluation-service` (`MLflow`): run metrics tracking.
- `observability stack`: Prometheus + Grafana for monitoring and alerting.
- `Full pipeline integration/e2e testing`.
- `Production security, deployment, and scaling`.


- Shared infrastructure: `redis` (Temporal), `seaweedfs` (artifacts), `postgres` (metadata).
- `temporal`, `temporal-ui`, `postgres`, `seaweedfs`, `mlflow`, `label-studio`, `prometheus`, `grafana`.


```
validation-service
   -> workflow-orchestrator
      -> human-review-console API
         -> Label Studio UI
```


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

3. Optional: start VLM fallback on a GPU host:

```bash
docker compose --profile gpu up -d --build
```

For Apple Silicon/MPS local development, keep deterministic extraction enabled and do not start the `gpu` profile:

```env
EXTRACTION_ENABLE_VLM_FALLBACK=false
```

If you later want local fallback on Apple Silicon, point `VLLM_BASE_URL` at an OpenAI-compatible Ollama or LM Studio server instead of the Docker `vllm` service.

4. Submit a document:

```bash
curl -X POST "http://localhost:8081/documents" \
  -H "X-API-Key: dev-ingestion-key" \
  -H "X-Tenant-Id: default" \
  -H "X-Actor-Id: local-client" \
  -H "Idempotency-Key: local-smoke-001" \
  -F "file=@/absolute/path/to/document.png"
```

5. Check workflow status:

```bash
curl "http://localhost:8081/documents/<job_id>" \
  -H "X-API-Key: dev-ingestion-key" \
  -H "X-Tenant-Id: default"
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

## Full Pipeline E2E Testing

The full-pipeline e2e runner submits a real document to `ingestion_service`, waits for the Temporal workflow to finish, fetches the workflow result, validates every major stage contract, and writes diagnostic artifacts for review.

- Sample file: `samples/documents/sample_invoice_001.png`
- Sample generator: `scripts/generate_sample_invoice.py`
- E2E wrapper: `scripts/run_e2e.sh`
- Gateway E2E wrapper: `scripts/run_gateway_e2e.sh`
- Python harness: `scripts/full_pipeline_e2e.py`
- Artifact output: `artifacts/e2e/<run-id>/`

The runner validates these stages:
- `preprocess`: preprocessed artifact key exists.
- `ocr`: OCR artifact key and mean confidence exist.
- `layout`: layout artifact key exists.
- `classification`: routing profile exists.
- `extraction`: structured fields, extraction artifact, confidence, and fallback flag exist.
- `validation`: verdict and human-review decision exist.
- `delivery` branch: delivery status, delivery ID, and receipt key exist.
- `human review` branch: review task status and review task ID exist.

Run against the live stack:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default ./scripts/run_gateway_e2e.sh
```

Use a custom document:

```bash
./scripts/run_e2e.sh /absolute/path/to/your_document.png
```

Tune runtime behavior:

```bash
API_URL=http://localhost:8081 TIMEOUT_SECONDS=900 POLL_INTERVAL=5 ./scripts/run_gateway_e2e.sh
```

Enable stricter assertions:

```bash
# Fail when the invoice required fields are missing, even if the workflow routes to HITL.
STRICT_REQUIRED_FIELDS=1 ./scripts/run_gateway_e2e.sh

# Also require Prometheus to expose at least one live IDP pipeline target.
E2E_REQUIRE_OBSERVABILITY=1 PROMETHEUS_URL=http://localhost:9090 ./scripts/run_gateway_e2e.sh
```

Useful artifact files after a run:
- `config.json`: redacted runner configuration.
- `submission.json`: ingestion submission response.
- `status_history.json`: every status poll with timestamps.
- `final_status.json`: last ingestion status response.
- `result.json`: final Temporal workflow result.
- `summary.json`: compact success summary.
- `validation_errors.json`: contract failures, when present.
- `logs/*.log`: Docker Compose log tails collected on failure by default.

## Dataset Benchmarking

Use dataset benchmarks after the smoke e2e passes. The benchmark runner submits many samples through the same live IDP pipeline, compares extraction output against ground truth, writes per-sample metrics, and optionally tracks aggregate metrics in `evaluation-service`.

- Dataset strategy: `data/README.md`
- CORD-v2 notes: `data/raw/cord-v2/README.md`
- Runner: `scripts/run_dataset_benchmark.py`

Install benchmark dependencies:

```bash
uv sync --frozen --no-default-groups --group research
```

Run a small CORD-v2 validation benchmark:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default \
  .venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 5 \
  --api-url http://localhost:8081 \
  --evaluation-url http://localhost:8018
```

Run without submitting documents:

```bash
.venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 3 \
  --dry-run \
  --no-track-evaluation
```

Run a private gold manifest:

```bash
python3 scripts/run_dataset_benchmark.py \
  --manifest data/benchmarks/invoice_gold_manifest.jsonl \
  --split validation \
  --limit 10
```

Benchmark artifacts are written to `artifacts/benchmarks/<dataset>-<split>-<run-id>/` and are git-ignored by default.

## Live Stack E2E Procedure

Use this for real end-to-end verification against running containers, Temporal state, object storage, delivery/HITL branching, and optionally Prometheus.

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
curl -fsS http://localhost:8081/health
curl -fsS http://localhost:8088
```

4. Run the full pipeline e2e test:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default ./scripts/run_gateway_e2e.sh
```

5. Expected success indicators:
- Script prints `E2E test completed successfully.`
- `workflow_final_status` in `summary.json` is `delivered` or `pending_human_review`.
- Temporal UI shows workflow execution in `COMPLETED` state.
- If final status is `delivered`, delivery receipt fields are present.
- If final status is `pending_human_review`, a Label Studio or local queue review task is present.
- Missing invoice required fields are only fatal by default when the workflow claims final delivery.
- Set `STRICT_REQUIRED_FIELDS=1` to fail e2e on any missing invoice required field.

6. Inspect workflow execution in Temporal UI:
- Open [http://localhost:8088](http://localhost:8088)
- Namespace: `default`
- Task queue: `idp-pipeline`

7. If test fails, inspect the generated artifacts first:

```bash
ls -R artifacts/e2e
cat artifacts/e2e/<run-id>/summary.json
cat artifacts/e2e/<run-id>/validation_errors.json
```

8. Then check live service state:

```bash
docker compose logs --tail=200 ingestion-service
docker compose logs --tail=200 workflow-orchestrator
docker compose logs --tail=200 preprocess-worker ocr-service layout-service classifier-router-service extraction-service validation-service human-review-console delivery-service evaluation-service
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

- Public API Gateway: [http://localhost:8081](http://localhost:8081)
- Ingestion API direct debug port: [http://localhost:8000](http://localhost:8000)
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
- `extraction-service` runs deterministic extraction first. VLM fallback is optional and disabled by default for Apple Silicon/local CPU efficiency.
- HITL tasks are sent to Label Studio when validation gates fail.
