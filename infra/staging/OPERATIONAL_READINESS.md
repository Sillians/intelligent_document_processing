# Staging Operational Readiness

This milestone proves staging reliability with repeatable drills and saved
artifacts. Production deployment remains manual until these drills are boring.

For the complete ordered deployment, functional, resilience, observability,
quality, hardening, and promotion procedure, use
[`DEPLOYMENT_VALIDATION.md`](DEPLOYMENT_VALIDATION.md).

Artifacts are written under:

```text
artifacts/staging/<drill>/<timestamp>/
```

The drill runner is:

```bash
python3 scripts/staging_operational_drill.py --help
```

## 1. Post-Deploy Smoke

Run after every staging deployment.

```bash
python3 scripts/staging_operational_drill.py smoke \
  --api-url https://staging-idp.example.com \
  --api-key "$STAGING_SMOKE_API_KEY" \
  --tenant-id default \
  --prometheus-url https://staging-prometheus.example.com \
  --require-observability
```

**Proof:**

- `drill_manifest.json`
- full e2e artifacts from `scripts/full_pipeline_e2e.py`
- `summary.json`
- `status_history.json`
- `result.json`

**Passing criteria:**

- gateway `/health` is reachable through the public API URL,
- a document can be submitted,
- status polling reaches `COMPLETED`,
- result fetch passes the pipeline contract,
- Prometheus has at least one live IDP pipeline scrape target.

## 2. Backup Drill

Run before staging upgrades and at least weekly.

```bash
python3 scripts/staging_operational_drill.py backup \
  --env-file .env.staging
```

**The drill captures:**

- `postgres.sql`
- stateful Compose volume archives:
  - `postgres_data`
  - `seaweedfs_data`
  - `labelstudio_data`
  - `grafana_data`
  - `prometheus_data`
  - `alertmanager_data`
- `backup_manifest.json`

**Passing criteria:**

- Postgres dump is non-empty.
- Every selected volume archive is created and non-empty.
- Backup manifest records byte sizes and volume names.


## 3. Restore Verification Drill

Run immediately after a backup drill.

```bash
python3 scripts/staging_operational_drill.py restore-verify \
  --backup-dir artifacts/staging/backup/<timestamp>
```

This verifies that the backup set is readable without modifying the running
staging stack.

**Proof:**

- `restore_verify_manifest.json`
- tar archive entry counts and sample paths
- Postgres dump marker check

**Passing criteria:**

- `postgres.sql` exists and contains database dump markers.
- At least one volume archive can be listed with `tar`.
- The restore verification manifest is written.

A full destructive restore into an isolated host should be performed before
production CD is considered.

## 4. Rollback Drill

Run after deploying a known-good release candidate and a newer candidate.

```bash
python3 scripts/staging_operational_drill.py rollback \
  --env-file .env.staging \
  --image-registry ghcr.io/<owner>/<repo> \
  --image-tag <previous-known-good-sha> \
  --api-url https://staging-idp.example.com \
  --api-key "$STAGING_SMOKE_API_KEY" \
  --tenant-id default
```

**Proof:**

- `compose_ps_before.jsonl`
- `compose_ps_after.jsonl`
- `rollback_manifest.json`
- optional nested smoke artifacts

**Passing criteria:**

- previous image tag is pulled,
- gateway-facing services restart with the rollback tag,
- post-rollback smoke passes.


## 5. Alert Drill

Run only on staging. This drill intentionally stops a service long enough for
Prometheus and Alertmanager to fire.

```bash
python3 scripts/staging_operational_drill.py alert \
  --env-file .env.staging \
  --service delivery-service \
  --alertmanager-url https://staging-alertmanager.example.com \
  --wait-seconds 150 \
  --confirm
```

**Proof:**

- `compose_ps_before.jsonl`
- `alertmanager_alerts.json`
- `compose_ps_after.jsonl`
- `alert_drill_manifest.json`

**Passing criteria:**

- `IDPServiceDown` appears in Alertmanager for the stopped service.
- The service is restarted by the drill.
- The alert resolves after recovery.

## Promotion Gate

Production CD must stay disabled until staging has recent evidence for:

- post-deploy smoke,
- backup drill,
- restore verification drill,
- rollback drill,
- alert drill.

Recommended evidence window: the last 7 days, or every release candidate if release cadence is slower than weekly.


---

