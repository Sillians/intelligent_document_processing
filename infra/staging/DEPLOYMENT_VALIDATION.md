# Staging Deployment Validation Runbook

Use this runbook after a release candidate is deployed to staging. A green
deployment job proves that the deployment path worked; it does not, by itself,
prove that the IDP application is functionally or operationally ready.

Record the following for every validation run:

- release commit SHA and image tag,
- GitHub Actions deployment run URL,
- operator and validation date,
- smoke and benchmark artifact names,
- outcome of each gate in the checklist below.

## Passing Gates

| Gate | What it proves | Pass condition |
| --- | --- | --- |
| 1. Release identity | The intended immutable build was deployed | Running application images use the selected commit SHA |
| 2. Deployment health | Compose, dependencies, and services started correctly | Required containers are running and application health checks are healthy |
| 3. Functional smoke | One document completes the real public pipeline | Workflow reaches `COMPLETED`; every stage contract passes; final branch is `delivered` or `pending_human_review` |
| 4. Representative documents | The pipeline handles more than the synthetic smoke sample | Representative invoices and CORD-v2 samples complete and produce reviewable results |
| 5. API and resilience | Authentication, idempotency, recovery, fallback, retry, and HITL behavior work | Every test below has the expected response or recovery outcome |
| 6. Rollback | A previous release can be restored safely | Previous SHA starts and its post-rollback smoke passes |
| 7. Observability | Operators can detect and diagnose failures | Prometheus targets are up, dashboards load, notifications work, and required alert rules exist |
| 8. Quality and performance | The release satisfies measurable acceptance limits | Benchmark thresholds pass and latency/throughput remain within the approved baseline |
| 9. Promotion readiness | The exact tested release is safe to promote | All required evidence is recent and the image SHA has not changed |

Do not approve production promotion from Gate 1 or Gate 2 alone.

## Before You Start

The staging GitHub environment must contain:

```text
STAGING_ENV_FILE
STAGING_PUBLIC_BASE_URL
STAGING_SMOKE_API_KEY
```

Recommended values for a staging runner that exposes the gateway locally:

```text
STAGING_PUBLIC_BASE_URL=http://127.0.0.1:8081
STAGING_TENANT_ID=default
```

`STAGING_ENV_FILE` must contain the complete staging environment file. Updating
a checkout's local `.env.staging` does not update the GitHub secret.

The post-deploy workflow deliberately skips smoke validation when
`STAGING_PUBLIC_BASE_URL` or `STAGING_SMOKE_API_KEY` is missing. Therefore,
inspect the `Run post-deploy staging smoke` step and confirm that it ran; a
message beginning with `Skipping staging smoke` is not a functional pass.

For commands run directly on the staging host, initialize this shell context:

```bash
export STAGING_APP_DIR="${STAGING_APP_DIR:-$HOME/idp-staging}"
cd "$STAGING_APP_DIR/current"

export IDP_IMAGE_REGISTRY="ghcr.io/<owner>/<repository>"
export IDP_IMAGE_TAG="<deployed-commit-sha>"
export STAGING_PUBLIC_BASE_URL="http://127.0.0.1:8081"
export STAGING_SMOKE_API_KEY="<staging-api-key>"
export STAGING_TENANT_ID="default"

compose=(
  docker compose
  --env-file .env.staging
  -f docker-compose.yml
  -f docker-compose.prod.yml
  -f docker-compose.staging.yml
)
```

On Linux, `STAGING_APP_DIR` normally defaults to `/opt/idp-staging`.

## Gate 1: Verify the Release Identity

The automatic path is:

```text
main commit
  -> Release Candidate workflow publishes SHA-tagged images
  -> Deploy Staging workflow deploys those same SHA-tagged images
```

The `Release Candidate` workflow must succeed before the automatic staging
workflow runs. For a manual deployment, dispatch `Deploy Staging` and supply
the exact SHA tag to test.

On the staging host, confirm the running image references:

```bash
docker ps \
  --filter label=com.docker.compose.project=idp-staging-stack \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

Pass when:

- all release application images use `<deployed-commit-sha>`,
- no application service uses `latest` or `staging-candidate`,
- the SHA matches the GitHub Actions deployment metadata.

Save the deployment run URL and the output above with the release evidence.

## Gate 2: Verify Deployment and Service Health

Validate the rendered configuration:

```bash
"${compose[@]}" config --quiet
```

Inspect all staging containers:

```bash
"${compose[@]}" ps
curl -fsS "$STAGING_PUBLIC_BASE_URL/health"
```

The deployment workflow starts Postgres, Redis, SeaweedFS, Temporal, the
gateway, MLflow, Label Studio, and all IDP application services. It also creates
the `idp`, `temporal`, `temporal_visibility`, `mlflow`, and `labelstudio`
databases when missing.

Pass when:

- Compose configuration validation exits with code `0`,
- the gateway health request exits with code `0`,
- required containers are running,
- health-checked IDP application containers report `healthy`,
- no container is repeatedly restarting.

If a service is unhealthy, inspect it before continuing:

```bash
SERVICE_NAME="ocr-service"
CONTAINER_ID="$("${compose[@]}" ps -q "$SERVICE_NAME")"
"${compose[@]}" logs --tail=200 "$SERVICE_NAME"
docker inspect "$CONTAINER_ID"
```

Gate 2 proves deployment health only. Continue to Gate 3.

## Gate 3: Run the Full Functional Smoke

Run the same smoke harness used by the deployment workflow:

```bash
python3 scripts/staging_operational_drill.py smoke \
  --api-url "$STAGING_PUBLIC_BASE_URL" \
  --api-key "$STAGING_SMOKE_API_KEY" \
  --tenant-id "$STAGING_TENANT_ID" \
  --artifact-dir "artifacts/staging/smoke/$IDP_IMAGE_TAG"
```

The harness submits a document through the public gateway, waits for Temporal,
fetches the result, and validates:

- preprocessing artifact,
- OCR artifact and mean confidence,
- layout artifact,
- classification route,
- extraction fields, artifact, confidence, and fallback flag,
- validation verdict and human-review decision,
- delivery receipt or human-review task.

Pass when:

- the command exits with code `0`,
- `drill_manifest.json` contains `"passed": true`,
- `final_status.json` reports workflow status `COMPLETED`,
- `summary.json` reports `workflow_final_status` as `delivered` or
  `pending_human_review`,
- `validation_errors.json` is absent.

Inspect:

```bash
python3 -m json.tool "artifacts/staging/smoke/$IDP_IMAGE_TAG/drill_manifest.json"
python3 -m json.tool "artifacts/staging/smoke/$IDP_IMAGE_TAG/summary.json"
python3 -m json.tool "artifacts/staging/smoke/$IDP_IMAGE_TAG/result.json"
```

In GitHub Actions, download the artifact named
`staging-smoke-<deployed-commit-sha>` and review the same files. Do not treat the
presence of an artifact as a pass; artifacts are uploaded even after failures.

## Gate 4: Test Representative Documents

### Invoices

Run the smoke harness against each representative invoice. Use a unique
artifact directory for every sample:

```bash
python3 scripts/staging_operational_drill.py smoke \
  --api-url "$STAGING_PUBLIC_BASE_URL" \
  --api-key "$STAGING_SMOKE_API_KEY" \
  --tenant-id "$STAGING_TENANT_ID" \
  --sample-path samples/documents/sample_invoice_001.png \
  --artifact-dir "artifacts/staging/documents/$IDP_IMAGE_TAG/sample-invoice"
```

Repeat with real, sanitized test documents that cover:

- clean digital invoices,
- scanned and rotated invoices,
- low-resolution or noisy images,
- multi-vendor layouts,
- missing required fields,
- documents expected to route to human review.

For documents intended for automatic delivery, confirm that
`invoice_number`, `invoice_date`, and `total_amount` are populated and that the
delivery receipt is present. For low-confidence documents, confirm that a
review task is present.

### CORD-v2

Install the research dependency group once on the staging validation host:

```bash
uv sync --frozen --no-default-groups --group research
```

Run a small CORD-v2 validation set:

```bash
INGESTION_API_KEY="$STAGING_SMOKE_API_KEY" \
TENANT_ID="$STAGING_TENANT_ID" \
.venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 5 \
  --api-url "$STAGING_PUBLIC_BASE_URL" \
  --no-track-evaluation \
  --pipeline-version "$IDP_IMAGE_TAG"
```

Artifacts are written under:

```text
artifacts/benchmarks/cord-v2-validation-<run-id>/
```

Pass the initial functional gate when every selected sample completes without a
runtime error and `pipeline_contract_pass_rate` is `1.0`. Record route accuracy,
field F1, OCR confidence, and human-review rate as the baseline; Gate 8 applies
the approved quality thresholds.

The staging overlay currently sets `OCR_FORCE_FALLBACK=true`. CORD-v2 may
therefore complete through HITL with low extraction scores. This proves control
flow and fallback behavior, not production OCR quality.

## Gate 5: Validate API and Resilience Behavior

Run these tests in staging only. Use sanitized documents and unique request
identifiers.

### Invalid API key

```bash
curl -sS -o /tmp/staging-invalid-key.json -w '%{http_code}\n' \
  -X POST "$STAGING_PUBLIC_BASE_URL/documents" \
  -H 'X-API-Key: deliberately-invalid' \
  -H "X-Tenant-Id: $STAGING_TENANT_ID" \
  -H 'X-Actor-Id: staging-negative-test' \
  -H 'Idempotency-Key: staging-invalid-key-test' \
  -F 'file=@samples/documents/sample_invoice_001.png'

python3 -m json.tool /tmp/staging-invalid-key.json
```

Pass when the HTTP status is `401` and the error envelope identifies invalid
credentials. No job should be created.

### Tenant isolation

Submit or query with a valid API key and a tenant header that does not match
the tenant assigned to that key.

Pass when the API returns HTTP `403` with a tenant-scope violation and does not
expose another tenant's job or result.

### Idempotent replay

Use a previously unsubmitted document and a unique key:

```bash
export IDEMPOTENCY_KEY="staging-$IDP_IMAGE_TAG-invoice-001"

for attempt in 1 2; do
  curl -fsS \
    -X POST "$STAGING_PUBLIC_BASE_URL/documents" \
    -H "X-API-Key: $STAGING_SMOKE_API_KEY" \
    -H "X-Tenant-Id: $STAGING_TENANT_ID" \
    -H 'X-Actor-Id: staging-idempotency-test' \
    -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
    -F 'file=@/absolute/path/to/previously-unsubmitted-document.png' \
    -o "/tmp/staging-idempotency-$attempt.json"
done

python3 -m json.tool /tmp/staging-idempotency-1.json
python3 -m json.tool /tmp/staging-idempotency-2.json
```

Pass when both responses contain the same `job_id`, the first response has
`idempotency_replay: false`, and the second has `idempotency_replay: true`.
Submitting the same file under a different key within the deduplication window
should return the existing job with `deduplicated: true`.

### OCR failure and HITL routing

The staging overlay intentionally forces deterministic OCR fallback. First run
a representative low-confidence document and inspect:

```bash
python3 -m json.tool "artifacts/staging/documents/$IDP_IMAGE_TAG/<sample>/summary.json"
python3 -m json.tool "artifacts/staging/documents/$IDP_IMAGE_TAG/<sample>/result.json"
```

Pass when fallback is visible as `ocr_fallback_used: true`, the workflow still
reaches `COMPLETED`, and unsafe output is routed to
`pending_human_review` with a review task rather than silently delivered.

To test network-level OCR fallback, stop `ocr-service`, submit a unique
document, and start the service again:

```bash
"${compose[@]}" stop ocr-service

# Run the Gate 3 smoke command with a unique document and artifact directory.

"${compose[@]}" up -d ocr-service
```

`ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK` must remain enabled for this test.
Pass when the result records OCR fallback, the document takes the safe review
path, and `ocr-service` returns healthy after restart.

### Temporal retry

Temporal activities use at most three attempts, beginning with a two-second
retry interval. To observe a real retry, briefly stop a non-OCR stage, submit a
unique document, and restore the stage before the attempts are exhausted:

```bash
"${compose[@]}" stop classifier-router-service

# Immediately submit a unique document in another shell.

"${compose[@]}" up -d classifier-router-service
```

Inspect the workflow history and orchestrator logs:

```bash
"${compose[@]}" logs --since=10m workflow-orchestrator classifier-router-service
```

Pass when the workflow history shows a failed activity attempt followed by a
successful attempt and the workflow completes. If all attempts are exhausted,
the expected result is a traceable failed workflow, not partial delivery.

### Container restart recovery

Restart one application service at a time:

```bash
"${compose[@]}" restart ingestion-service
"${compose[@]}" restart workflow-orchestrator
```

After each restart, wait for health and rerun Gate 3. Pass when persisted jobs
remain queryable, workers reconnect to Temporal, and a new smoke run completes.
Do not restart all stateful dependencies simultaneously during this test.

## Gate 6: Run the Rollback Drill

Identify a previous known-good commit SHA, then run:

```bash
python3 scripts/staging_operational_drill.py rollback \
  --env-file .env.staging \
  --image-registry "$IDP_IMAGE_REGISTRY" \
  --image-tag "<previous-known-good-sha>" \
  --api-url "$STAGING_PUBLIC_BASE_URL" \
  --api-key "$STAGING_SMOKE_API_KEY" \
  --tenant-id "$STAGING_TENANT_ID"
```

Pass when:

- the previous SHA-tagged images are pulled,
- gateway-facing application services restart on that SHA,
- the nested post-rollback smoke passes,
- `rollback_manifest.json` records a successful drill.

Save `compose_ps_before.jsonl`, `compose_ps_after.jsonl`,
`rollback_manifest.json`, and the nested smoke artifacts.

After the drill, explicitly decide whether staging should remain on the
known-good SHA or be redeployed to the candidate SHA.

## Gate 7: Enable and Validate Observability

The current staging deployment workflow does not start Prometheus, Alertmanager,
or Grafana. Start them on the staging host:

```bash
"${compose[@]}" up -d prometheus alertmanager grafana
"${compose[@]}" ps prometheus alertmanager grafana

curl -fsS http://127.0.0.1:9090/-/healthy
curl -fsS http://127.0.0.1:9093/-/healthy
curl -fsS http://127.0.0.1:3000/api/health
```

Set this GitHub staging environment secret only after Prometheus is reachable
from the self-hosted runner:

```text
STAGING_PROMETHEUS_URL=http://127.0.0.1:9090
```

Rerun Gate 3 with observability required:

```bash
python3 scripts/staging_operational_drill.py smoke \
  --api-url "$STAGING_PUBLIC_BASE_URL" \
  --api-key "$STAGING_SMOKE_API_KEY" \
  --tenant-id "$STAGING_TENANT_ID" \
  --prometheus-url http://127.0.0.1:9090 \
  --require-observability \
  --artifact-dir "artifacts/staging/smoke/$IDP_IMAGE_TAG-observability"
```

Query live IDP targets:

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{tier="idp-pipeline"}'
```

Run the alert drill:

```bash
python3 scripts/staging_operational_drill.py alert \
  --env-file .env.staging \
  --service delivery-service \
  --alertmanager-url http://127.0.0.1:9093 \
  --wait-seconds 150 \
  --confirm
```

Pass when:

- the smoke summary reports at least one live IDP target,
- Grafana loads the provisioned IDP dashboard,
- `IDPServiceDown` fires for the stopped service and resolves after recovery,
- the configured notification receiver receives the test alert,
- health, HTTP 5xx rate, client error rate, and p95 latency rules evaluate
  without errors.

The current `infra/prometheus/alerts.yml` does not define queue-depth or
disk-space alerts. Those two alert rules and their metrics must be implemented
and tested before claiming the complete observability gate requested for
production readiness.

## Gate 8: Benchmark Quality and Performance

Start with explicit release thresholds. The benchmark runner can enforce:

- minimum completion rate,
- minimum pipeline-contract pass rate,
- minimum route accuracy,
- minimum field F1,
- maximum human-review rate.

Example only; replace these values with approved baselines:

```bash
INGESTION_API_KEY="$STAGING_SMOKE_API_KEY" \
TENANT_ID="$STAGING_TENANT_ID" \
.venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 20 \
  --api-url "$STAGING_PUBLIC_BASE_URL" \
  --no-track-evaluation \
  --pipeline-version "$IDP_IMAGE_TAG" \
  --min-completion-rate 1.0 \
  --min-contract-pass-rate 1.0 \
  --min-route-accuracy 0.90 \
  --min-field-f1 0.80 \
  --max-human-review-rate 0.30
```

Record:

- OCR CER/WER from the selected OCR evaluation dataset,
- extraction field precision, recall, and F1,
- validation accuracy,
- completion and human-review rates,
- documents per minute,
- end-to-end p50 and p95 latency,
- test concurrency and host resource profile.

Do not use the example threshold values as production acceptance criteria
without an approved baseline. Performance comparisons are valid only when the
dataset, concurrency, OCR mode, and staging resources are held constant.

Because staging currently forces OCR fallback, production OCR quality cannot be
approved from this profile. Repeat the quality gate with the intended OCR engine
on suitable infrastructure before production promotion.

## Gate 9: Hardening and Promotion Checklist

Before promotion, verify:

- staging API keys and database credentials are high-entropy and rotated,
- tenant isolation tests pass,
- external traffic uses encrypted transport,
- backup and restore-verification drills pass,
- retention settings and audit records meet policy,
- no secrets appear in logs or evidence artifacts,
- rollback and alert drills have recent passing evidence,
- production uses separate infrastructure, secrets, and persistent storage,
- the production environment requires manual approval,
- production deploys the exact tested image SHA.

Run backup and restore verification:

```bash
python3 scripts/staging_operational_drill.py backup \
  --env-file .env.staging

python3 scripts/staging_operational_drill.py restore-verify \
  --backup-dir "artifacts/staging/backup/<timestamp>"
```

Promotion is approved only when Gates 1 through 9 have the evidence required by
the release policy. Recommended evidence age is seven days or less, or one
complete validation set per release candidate when releases are less frequent.

## Evidence Package

Attach or link:

- GitHub `Release Candidate` run,
- GitHub `Deploy Staging` run,
- `staging-smoke-<sha>` artifact,
- representative-document artifacts,
- CORD-v2 benchmark `summary.json` and sample metrics,
- rollback drill artifact,
- alert drill artifact,
- backup and restore-verification artifacts,
- dashboard screenshots or exported queries,
- signed checklist containing the tested SHA and acceptance decision.

Keep secrets, raw customer documents, and unrestricted logs out of the evidence
package.

## Failure Triage

Start with the artifact for the failed gate:

```bash
find artifacts/staging -maxdepth 4 -type f | sort
```

For functional failures, inspect:

```text
submission.json
status_history.json
final_status.json
result.json
summary.json
validation_errors.json
logs/
```

Then inspect current service state and logs:

```bash
"${compose[@]}" ps
"${compose[@]}" logs --tail=200 \
  ingestion-service \
  workflow-orchestrator \
  preprocess-worker \
  ocr-service \
  layout-service \
  classifier-router-service \
  extraction-service \
  validation-service \
  human-review-console \
  delivery-service
```

Do not promote a release with an unexplained intermittent failure. Record the
root cause, corrective action, rerun evidence, and final decision.
