# Production Operations Runbook

This runbook hardens the existing Docker Compose architecture before any Kubernetes migration. Compose remains the single-host deployment mechanism; Minikube or Kubernetes should only come after these contracts are proven.

## Production Contract

- The Traefik gateway is the only public application entry point.
- Traefik routes only `/documents*` and `/health` to `ingestion-service`.
- `ingestion-service` stays internal-only in production; do not publish it directly.
- All other ports stay bound to localhost, a private interface, or a VPN-only reverse proxy.
- Every stateful service has an owner, backup schedule, and restore test.
- Secrets come from `.env.production` or a host secret manager, never from `.env.example`.
- Pipeline services stay stateless; artifacts live in S3-compatible storage and metadata lives in Postgres.
- Deployments pass preflight, smoke e2e, and observability checks before promotion.

## Files

- `.env.production.example`: production environment template with placeholder values only.
- `docker-compose.prod.yml`: production override for bind addresses, container hardening, log rotation, pids, CPU, and memory envelopes.
- `docker-compose.release.yml`: release-image override for SHA-tagged GHCR images.
- `scripts/production_preflight.py`: fail-fast checks for unsafe production settings.
- `infra/api/PUBLIC_API.md`: public API contract for client/API consumers.
- `infra/traefik/traefik.yml`: Traefik static gateway configuration.
- `infra/production/DCompose-k8s.md`: Kubernetes migration notes; Minikube is a rehearsal target only.
- `infra/staging/OPERATIONAL_READINESS.md`: staging smoke, backup, restore
  verification, rollback, and alert drills required before production CD.

## First-Time Setup

1. Copy and protect the production env file:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

2. Replace every `changeme` value with high-entropy secrets from a password manager or secret manager.

3. Keep public access narrow. The production overlay exposes Traefik as the API gateway and removes direct host publishing from `ingestion-service`.

4. Run preflight:

```bash
python3 scripts/production_preflight.py --env-file .env.production
```

5. Build and start:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

6. Confirm health:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml ps
```

7. Run the live smoke test:

```bash
INGESTION_API_KEY=<production-smoke-api-key> TENANT_ID=default API_URL=https://<public-idp-host> ./scripts/run_gateway_e2e.sh
```

## Security Controls

- `INGESTION_REQUIRE_AUTH=true` is mandatory.
- `INGESTION_API_KEYS` must use `api-key:tenant-id` pairs with high-entropy keys.
- Rotate ingestion keys by adding the new key, updating clients, then removing the old key.
- Traefik handles TLS termination, path allowlisting, request body limits, rate limits, response security headers, health checks, and JSON access logs.
- API key validation remains in `ingestion-service`. For JWT/OIDC at the gateway, add a forward-auth service and Traefik middleware.
- Keep `DELIVERY_WEBHOOK_REQUIRE_SIGNATURE=true`.
- Keep `DELIVERY_ALLOW_REQUEST_WEBHOOK_URL=false` unless SSRF controls are reviewed.
- Configure `DELIVERY_WEBHOOK_URL` only for trusted tenant/event consumers.
- Disable anonymous Label Studio signup with `LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK=true`.
- Change Grafana, Postgres, S3, Label Studio, SMTP, webhook, and VLM credentials before first boot.
- Do not expose Postgres, Redis, SeaweedFS, Temporal, Prometheus, Alertmanager, Grafana, MLflow, or Label Studio directly to the internet.
- Run the app images as the non-root `app` user.
- Use TLS at the reverse proxy or load balancer.
- Store raw and derived document artifacts in tenant-scoped keys and keep audit events enabled.
- Do not log API keys, raw document content, extracted PII, webhook secrets, or Label Studio tokens.

## Deployment Flow

1. Confirm the desired SHA passed GitHub Actions CI, release-candidate image
   publication, staging deployment, and staging operational readiness checks.
2. Confirm the GitHub `production` environment has required reviewers.
3. Run `.github/workflows/deploy-production.yml` manually with:
   - `image_tag`: the immutable release-candidate SHA tag,
   - `staging_evidence_url`: URL to approved staging smoke/readiness evidence,
   - `change_ticket`: optional change or incident reference,
   - `run_predeploy_backup`: keep enabled unless an approved maintenance plan
     provides equivalent backup evidence.
4. Approve the workflow from the GitHub `production` environment gate.
5. The workflow runs production preflight, Compose validation, optional backup,
   deployment, production smoke, and artifact upload.
6. Watch startup if manual inspection is needed:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml ps
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml logs --tail=100 ingestion-service workflow-orchestrator
```

7. Record the deployed git SHA, env version, GitHub workflow run, and artifact
   path.

## Rollback

1. Keep the previous image revision available.
2. Run `.github/workflows/rollback-production.yml` manually with:
   - `image_tag`: the known-good SHA tag,
   - `reason`: rollback reason,
   - `change_ticket`: optional incident or change reference.
3. Approve the workflow from the GitHub `production` environment gate.
4. Confirm post-rollback smoke artifacts pass.
5. Restore the previous `.env.production` or data backups manually if the
   incident involved config or data migration.

## Backup And Restore

Back up these volumes before production upgrades and at a regular interval:

- `postgres_data`: ingestion metadata, Temporal metadata, MLflow DB, Label Studio DB.
- `seaweedfs_data`: raw files, intermediate artifacts, final outputs, MLflow artifacts.
- `labelstudio_data`: Label Studio local files and settings.
- `grafana_data`: Grafana state outside provisioned dashboards.
- `prometheus_data`: metrics history.
- `alertmanager_data`: alert silence/state data.

Minimum policy:

- Daily encrypted backup for Postgres and SeaweedFS.
- Weekly restore drill into an isolated host.
- Retain enough history to satisfy compliance and customer recovery requirements.
- Verify both metadata and artifacts during restore; a job record without matching object storage is not a valid recovery.

Suggested backup commands must be adapted to your storage provider and maintenance window:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml exec postgres pg_dumpall -U "$POSTGRES_USER" > backups/postgres-$(date +%Y%m%d%H%M).sql
docker run --rm -v idp-production-stack_seaweedfs_data:/data:ro -v "$PWD/backups:/backup" busybox tar czf /backup/seaweedfs-$(date +%Y%m%d%H%M).tgz /data
```

## Scaling

Use Compose scaling only for stateless pipeline workers. Do not scale singleton stateful services with this Compose setup.

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up -d \
  --scale workflow-orchestrator=2 \
  --scale preprocess-worker=3 \
  --scale ocr-service=2 \
  --scale extraction-service=2
```

Scaling guidance:

- `ingestion-service`: scale carefully because it owns API ingress and DB pools.
- `workflow-orchestrator`: increase with Temporal backlog and activity latency.
- `preprocess-worker`: CPU and memory heavy.
- `ocr-service`: CPU/GPU and memory heavy; start with low concurrency.
- `layout-service`: model memory heavy.
- `extraction-service`: scale with LLM or downstream latency.
- `validation-service`, `delivery-service`, `evaluation-service`: usually lightweight, scale by request latency and error rates.
- `postgres`, `redis`, `seaweedfs`, `temporal`, `mlflow`, `label-studio`, `prometheus`, `alertmanager`, and `grafana`: keep one replica in Compose. Move to managed services or Kubernetes operators for HA.

Tune these settings together:

- `*_MAX_INFLIGHT_REQUESTS`
- `TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES`
- service `*_MEM_LIMIT`
- Postgres pool sizes
- OCR/layout model memory

## Observability

Prometheus scrapes all FastAPI `/metrics` endpoints. Grafana is provisioned from `infra/grafana`, and Alertmanager uses `infra/alertmanager`.

Production alerts should page on:

- any critical service down,
- sustained critical-path 5xxs,
- high p95 latency,
- workflow queue publish failures,
- human-review backlog growth,
- delivery failures,
- missing Prometheus scrape targets.

Use these views during incidents:

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml logs --tail=200 ingestion-service
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml logs --tail=200 workflow-orchestrator
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml logs --tail=200 ocr-service extraction-service validation-service delivery-service
```

## Kubernetes Readiness

Do not migrate until this Compose production baseline is boring:

- preflight passes,
- smoke e2e passes,
- backups restore successfully,
- service metrics and alerts work,
- resource limits are tuned,
- secrets are rotated without downtime,
- scaling guidance is validated with realistic documents.

Once those are true, translate the same contracts into Kubernetes `Deployment`, `Service`, `ConfigMap`, `Secret`, `PVC`, `Ingress`, and Helm/Kustomize values. Minikube is useful for testing that packaging, not for production.
