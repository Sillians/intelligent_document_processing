# Live milestone verification

## Page Retry Gate (2026-10-03)
Start this profile first (build its shared image once if absent), then run:

```bash
docker --context idp-staging compose -f infra/verification/compose.yml up -d --no-build
docker --context idp-staging compose -f infra/verification/compose.yml exec -T ingestion-service \
  python -m scripts.verify_page_execution
docker --context idp-staging compose -f infra/verification/compose.yml exec -T ingestion-service \
  python -m scripts.replay_pipeline_histories artifacts/live-milestone artifacts/recovery-delivery artifacts/page-execution \
  --report artifacts/page-execution/replay.json
```

The isolated drill uses a unique task queue and a controlled retryable page-two
activity failure, stops its worker gracefully, and starts a new worker. Real
preprocessing/OCR/layout must complete with page-one OCR called once and page-two
activity called twice. Actual Label Studio correction, validation and delivery
then complete. Inspect `artifacts/page-execution/report.json`, ordered stage
artifacts and the exported history; only an actual `passed:true` is evidence.
This is not a process-SIGKILL test, a representative quality benchmark or a
promoted-image test. The regular worker remains on its own task queue.

The 2026-10-03 run passed with OCR attempt counts `{1: 1, 2: 2}` and acknowledged
delivery after actual Label Studio approval. This profile omits evaluation-service;
its best-effort tracking call failed without blocking delivery. This run does not
verify MLflow tracking. See the report and replay report for current evidence.
The batch replay subsequently passed all 18 exported histories, including the new
page execution history and prior review/recovery/delivery histories, with exit 0.

During setup, the engine reported duplicate ghost endpoints for this project's
OCR/layout containers even after Compose recreation. Disconnecting while their
same-named containers existed did not repair the problem. The repair stopped and
removed only those two service containers (without volumes), force-disconnected
their orphan endpoints by the now-unused container names, then recreated the two
services. Inspection confirmed populated endpoint IDs and IP addresses before
the live drill. Startup used `COMPOSE_PARALLEL_LIMIT=1`.
Named volumes and other projects were preserved. Do not use global pruning to
resolve this problem. If it recurs, inspect this network and container IDs before
performing any targeted repair; a clean startup is still required for live tests.

## Batch History Compatibility

Replay exported old and new histories with one SDK worker and an explicitly
owned executor:

```bash
docker --context idp-staging compose -f infra/verification/compose.yml exec -T ingestion-service \
  python -m scripts.replay_pipeline_histories artifacts/live-milestone artifacts/recovery-delivery \
  --report artifacts/recovery-delivery/replay-regression.json
```

The regression run cleanly replayed 13 pre-patch histories and two new delivery
histories (15 total). An earlier ad hoc run created one replayer per history and
exited with signal 11 after all replay assertions passed. The batch helper avoids
that observed teardown failure and checks replay plus a clean process exit.
The exact native crash cause is not established. Keep this distinction in
evidence rather than count that failed process as a clean compatibility run.

## Exception Recovery And Delivery Gate (2026-10-01)

After initializing the profile, run from the repository root:

```bash
bash scripts/run_recovery_delivery_verification.sh
```

This gate restarts the modified source-mounted services, temporarily stops OCR
to create three real exceptions through public intake, restores OCR, and runs
tenant-scoped operator resolution. A committed replacement recovers after a
terminated child process before raw copy; corrupt source is denied. Two linked
generations complete actual OCR, authenticated Label Studio approval, corrected
validation and acknowledged delivery, preserving the original failures. Both
new histories are exported and SDK-replayed.

The runner then stops the delivery container while child processes exercise
the production ledger/provider methods around before-send, accepted-but-unacked,
and receipt-PUT crash windows. Expired leases reconcile; stale acknowledgements
are denied. A real signed receiver runs inside ingestion at port 18999 (not
published to the host). Its PostgreSQL transaction deduplicates tenant/key and
applies one effect; a lost acknowledgement causes two identical POSTs, not two
effects. Further checks cover partial failure, concurrent submissions, bounded
dead letters, tenant-denied redrive, failed-only redrive and changed-payload 409.
Finally the real delivery service restarts and drains committed work without a
caller; its pending/dead-letter/age metrics are checked.

All 12 check groups must pass in `artifacts/recovery-delivery/report.json`.
A prepare phase resets the report. The script restores OCR/delivery on exit and
leaves the isolated profile running. It preserves other projects/volumes. The
child-process crash windows exercise real production calls, not instrumented
HTTP server crashes. The fixture service uses a 3-claim budget and 1-second
reconciliation/polling; production defaults are 10 claims and 5/10 seconds.

Local operator fixtures: `local-verification-operator` for `verification` and
`local-verification-operator-other` for `other`, via `X-IDP-Operator-Key`.
Delivery queue operations also need `X-IDP-Internal-Key`:
`local-verification-internal`. Never deploy these fixture secrets. See affected
service READMEs for API semantics, migrations, retention, deadline handling and
at-least-once webhook requirements. This gate does not measure representative
accuracy, configure a project-wide webhook, or test promoted image binaries.

This profile runs the actual IDP Python services, Temporal 1.27.2, Postgres 16,
SeaweedFS 4.25, and Label Studio 1.20.0 on the `idp-staging` Docker context.
It is a functional development stack with its own Compose project, network and
persistent volumes. Application source is mounted from this checkout; no image
promotion or production-quality benchmark is implied by its results.

## Run

Install host OCR on Apple Silicon:

```bash
brew install tesseract
tesseract --version
```

The verification Dockerfile also installs real Tesseract in the service image.
Build once and run the complete drill from the repository root:

```bash
bash scripts/run_live_milestone.sh
```

Set `IDP_DOCKER_CONTEXT` if using another existing Docker context. The runner
builds one shared CPU image, starts the services, and waits for readiness. Existing
projects on the same engine have separate networks and volumes. Application
ports remain internal except the following loopback endpoints:

- Label Studio: `http://127.0.0.1:18080`, login `verification@example.local`, password `local-verification-password`.
- Ingestion API: `http://127.0.0.1:18000`, API key `local-verification-intake`, tenant `verification`.
- Temporal gRPC: `127.0.0.1:17233`.

All profile credentials are explicit local fixtures. Use the established staging
and production profiles and their separate credentials for deployment. The
Label Studio project is created by the drill on its first run.

## Verified behavior

The drill starts real document workflows using tenant-scoped raw artifacts, with
real preprocessing, CPU OCR, layout, classification, extraction, and validation.
It then:

1. Waits for a real Label Studio review task.
2. Records worker state, sends SIGKILL, and starts the worker again.
3. Creates an authenticated Label Studio annotation, checks the review event
   credential, and verifies durable decision replay.
4. Verifies corrected fields and reviewer identity in the delivered object and
   its receipt. Repeating review creation and delivery preserves their IDs.
5. Verifies rejection, invalid corrected values, and a real 60-second review
   timeout produce no delivery.
6. Exports each completed Temporal history and runs the SDK Replayer against
   the production workflow class.
7. Uses real Tesseract as secondary OCR with an unavailable Paddle primary.
   Low-confidence output is rejected. A clean fixture is accepted with engine
   version, coordinates, confidence, and primary-failure provenance persisted.
8. Stops primary OCR, verifies three activity attempts, reads the durable
   tenant-scoped exception, denies another tenant, and replays that history.
9. Restores primary OCR even if the outage test fails.

The annotation is submitted by the configured Label Studio account through its
API. The event endpoint re-fetches authoritative task data, and the workflow
resumes through polling. This verifies the decision integration; it does not
configure a project-wide Label Studio webhook dispatcher.

Results are in `artifacts/live-milestone/report.json`, corrected results and
receipts, OCR artifacts, worker state snapshots, and `*.history.json`. Only
`passed: true` after the outage phase means the complete drill passed. A new
prepare phase clears a previous pass. Fixtures exercise functional behavior;
representative real-document quality acceptance remains a separate gate.

Optional final provenance check after an OCR code change:

```bash
docker --context idp-staging compose -f infra/verification/compose.yml restart ocr-secondary
docker --context idp-staging compose -f infra/verification/compose.yml exec -T ingestion-service \
  python -m scripts.verify_live_milestone secondary-recheck
```

## Operations

Inspect or stop the development stack without deleting its data:

```bash
docker --context idp-staging compose -f infra/verification/compose.yml ps
docker --context idp-staging compose -f infra/verification/compose.yml logs --tail=100 workflow-orchestrator
docker --context idp-staging compose -f infra/verification/compose.yml stop
```

The stack is left running after verification. Evaluation/MLflow and Grafana are
not part of this narrowly scoped functional profile; the workflow's optional
metric export may log an unavailable evaluation endpoint without changing the
asserted review/delivery result.

The first build exposed insufficient Docker storage and duplicate exports of
one image. Building only `ingestion-service` now creates the image all services
reuse. SeaweedFS uses explicitly bounded 8 MB development volumes and retains a
64 MiB free-space guard; this configuration is only for the small verification
corpus. No unrelated images, containers or volumes were pruned.

## 2026-10-01: Intake and Tenant Verification

With this profile initialized, run the next functional gate from the repository:

```bash
bash scripts/run_ingestion_verification.sh
```

The runner uses this profile only, temporarily stops ingestion so its background
reconciler does not race intentionally abandoned records, and restores ingestion
on exit. Five child processes terminate immediately after registration, storage
PUT, corrupt PUT, outbox commit, or Temporal start. Recovery uses real Postgres,
SeaweedFS, and Temporal, including lease fencing and transactional audit checks.
These are process-crash windows around production calls, not instrumented HTTP
server crashes.

The HTTP phase then checks key aliases and 409 conflicts, four concurrent
equal-content submissions, tenant-scoped status/audit/results, equal keys/content
in different tenants, and missing credentials/foreign artifact rejection at
preprocess, OCR, layout, classifier, extraction, validation, review, and delivery.
The second fixture tenant is `other`, using intake key
`local-verification-intake-other` and reviewer key
`local-verification-review-other`. Neither fixture is a production credential.

Both `artifacts/ingestion-recovery/report.json` and
`artifacts/ingestion-recovery/tenant-boundaries.json` must show `passed: true`.
Reports reset before each phase so a failed rerun cannot retain that phase's old
pass. Verification workflows are cancelled after assertions; fixture metadata
and objects remain in this profile for inspection. See the ingestion README and
`infra/architecture/IMPLEMENTATION_ORDER.md` for migration, recovery, and next
milestone semantics.
