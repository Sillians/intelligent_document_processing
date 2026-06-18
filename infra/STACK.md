# IDP Local Stack Design

## End-to-End Control Flow

1. `ingestion-service` stores raw file in SeaweedFS (S3-compatible API) and starts Temporal workflow (`idp-<job_id>`).
2. `workflow-orchestrator` executes activities:
   - preprocess -> OCR -> layout -> classify -> extract -> validate
3. If validation fails thresholds/rules, workflow creates HITL task in Label Studio.
4. If validation passes, workflow delivers structured output.
5. Workflow emits run metrics to MLflow via `evaluation-service`.

## Confidence Gating Logic

- OCR produces `mean_confidence`.
- Extraction computes field confidence (`ocr + field coverage`) and optionally upgrades with VLM fallback.
- Validation gates on:
  - missing required fields
  - confidence below `AUTO_APPROVE_THRESHOLD`
- Gate failure => `pending_human_review`.

## Compose Deployment Profiles

- Default profile: full pipeline without `vllm` GPU runtime.
- `gpu` profile: enables `vllm` service.
- Production override: `docker-compose.prod.yml` keeps the same architecture but adds safer bind addresses, container hardening, log rotation, and resource envelopes.

## Operational Commands

Start everything:

```bash
docker compose up -d --build
```

Production preflight:

```bash
python3 scripts/production_preflight.py --env-file .env.production
```

Production Compose start:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Start with GPU VLM:

```bash
docker compose --profile gpu up -d --build
```

Scale critical workers:

```bash
docker compose up -d --scale preprocess-worker=3 --scale ocr-service=2 --scale extraction-service=2 --scale workflow-orchestrator=2
```

Stop:

```bash
docker compose down
```

Stop + delete volumes:

```bash
docker compose down -v
```

Production runbook:

```text
infra/production/README.md
```
