# Human Review Console

## Page Failure Context (2026-10-03)
Worker-authenticated `POST /review/ocr-exceptions` accepts an optional positive
`page_number`. Exhausted independent OCR page retries persist the failing original
document page alongside the typed code. This does not authorize a partial result
or change operator resolution semantics. Whole-document failures may omit it.
Page retry verification still completes an authenticated Label Studio decision
and corrected revalidation before acknowledged delivery; see the verification README.

## Operator Exception Inspection (2026-10-01)

`GET /review/ocr-exceptions?tenant_id=...&limit=50&offset=0` now exposes the
durable exception queue (maximum page size 200). Both list and single-record
GET accept the privileged `REVIEW_DECISION_API_KEY` worker credential or a
tenant-mapped `REVIEW_DECISION_API_KEYS` credential via `X-IDP-Review-Key`.
Mapped credentials cannot inspect another tenant. Only the worker credential
can record workflow OCR exceptions; listing does not grant mutation privileges.
Database reads end their transactions before returning pooled connections.

Operators normally use ingestion's `/exceptions` and `/exceptions/{job}/reprocess`
with a separately mapped `X-IDP-Operator-Key`; ingestion fetches review records
using its private worker credential. Original exception/decision records stay
immutable; their resolution and linked replacement job live in ingestion's
database table. A retry cannot turn a Label Studio rejection into approval.

The existing authenticated Label Studio integration remains authoritative:
re-fetch decisions, preserve corrected fields/reviewer identity, then resume
Temporal polling and revalidate before delivery. The new live gate completes
this path for two actual reprocessing generations and replays both histories:

```bash
bash scripts/run_recovery_delivery_verification.sh
```

See `infra/verification/README.md` and `artifacts/recovery-delivery/report.json`.
Continue using the run commands below; this is an API layer, not a new dashboard.
The fixture gate is not representative quality evidence or a production deploy.

## Core Purpose
Create and persist human-in-the-loop review tasks for documents that fail validation gates or require analyst confirmation before delivery.

## Implemented Core Functionalities
- Accepts review requests from `workflow_orchestrator` at `POST /review/tasks`.

- Creates deterministic review task IDs so Temporal retries are idempotent.

- Supports a modular provider interface for review backends.

- Uses `LabelStudioProvider` when `LABEL_STUDIO_TOKEN` is configured and `REVIEW_PROVIDER=auto` or `label_studio`.

- Uses `LocalQueueProvider` by default when Label Studio is not configured, so Apple Silicon/local development remains fully functional.

- Falls back to the local queue if Label Studio is temporarily unavailable and `REVIEW_FALLBACK_TO_LOCAL_QUEUE=true`.

- Persists task records to object storage for auditability and downstream review/retraining workflows.

- Returns the existing workflow-compatible fields: `review_status` and `review_ref`.

- Adds richer metadata: `review_task_id`, `review_task_key`, `provider`, and `priority`.

- Exposes `GET /review/tasks/{job_id}/{review_task_id}` to load persisted task metadata.


## Provider Model
The service is intentionally modular. A provider only needs to implement:

```python
async def create_task(task, *, review_task_id, payload) -> ProviderResult:
    ...
```

Current providers:
- `LocalQueueProvider`: persists review tasks locally in S3-compatible storage.
- `LabelStudioProvider`: imports tasks into Label Studio through its project import API.

Future providers can be added for custom reviewer apps, Jira, Zendesk, Slack approval flows, or queue systems without changing workflow contracts.

## Primary Inputs
- `job_id`
- `reasons`
- `fields`
- `confidence`
- `route` optional
- `verdict` optional
- `priority` optional
- `assignee` optional
- `extraction_bucket` optional
- `extraction_key` optional
- `validation_bucket` optional
- `validation_key` optional
- `metadata` optional

Example request:
```json
{
  "job_id": "job-123",
  "reasons": ["low_confidence:0.720", "missing_required_fields:total_amount"],
  "fields": {
    "invoice_number": "INV-001",
    "invoice_date": "2026-06-01"
  },
  "confidence": 0.72,
  "route": "invoice",
  "verdict": "needs_review",
  "extraction_bucket": "extraction-artifacts",
  "extraction_key": "jobs/job-123/extraction/result.json",
  "validation_bucket": "validation-artifacts",
  "validation_key": "jobs/job-123/validation/result.json"
}
```

## Primary Outputs
- `job_id`
- `review_status`
- `review_ref`
- `review_task_id`
- `review_bucket`
- `review_task_key`
- `provider`
- `priority`

Example response with local queue:
```json
{
  "job_id": "job-123",
  "review_status": "queued_without_label_studio",
  "review_ref": "review-abc123",
  "review_task_id": "review-abc123",
  "review_bucket": "review-artifacts",
  "review_task_key": "jobs/job-123/review/review-abc123.json",
  "provider": "local_queue",
  "priority": "medium"
}
```

Example response with Label Studio:
```json
{
  "job_id": "job-123",
  "review_status": "queued_in_label_studio",
  "review_ref": "42",
  "review_task_id": "review-abc123",
  "review_bucket": "review-artifacts",
  "review_task_key": "jobs/job-123/review/review-abc123.json",
  "provider": "label_studio",
  "priority": "medium"
}
```

## Interfaces
- `GET /health`
- `GET /health/provider` (verifies Label Studio authentication and project access)
- `POST /review/tasks`
- `GET /review/tasks/{job_id}/{review_task_id}`
- `GET /review/tasks/{job_id}/{review_task_id}/decision?tenant_id=<tenant>` (Temporal worker polling; `X-IDP-Review-Key: REVIEW_DECISION_API_KEY`)
- `POST /review/tasks/{job_id}/{review_task_id}/decision` (manual reviewer decision; tenant-specific `X-IDP-Review-Key`)

## Completing A Review
The Temporal workflow remains open while the decision API returns `pending`. Label Studio annotations are polled by the service; the first valid decision is stored immutably in PostgreSQL and mirrored to object storage. Approval merges corrected fields into the extraction result, runs validation again, and permits delivery only when the rules pass. Rejection and review timeout terminate without delivery.

For a local-queue review, submit a decision with the reviewer key mapped to that task's tenant. This API is an integration layer, not a reviewer dashboard. The `reviewer_id` is client-supplied and should be bound to an identity provider before exposing manual decisions to untrusted reviewers.

```bash
curl -X POST http://localhost:8016/review/tasks/job-123/review-abc123/decision \
  -H 'Content-Type: application/json' \
  -H "X-IDP-Review-Key: ${REVIEWER_KEY}" \
  -d '{"tenant_id":"default","reviewer_id":"analyst-1","decision":"approved","fields":{"invoice_number":"INV-42","total_amount":"42.00"}}'
```

`REVIEW_DECISION_API_KEYS` is a comma-separated list of `key:tenant` entries for manual reviewers. `REVIEW_DECISION_API_KEY` is a separate worker-only secret used to poll decisions; neither key should be the public ingestion API key. Set `IDP_REQUIRE_INTERNAL_AUTH=true` and a strong `IDP_INTERNAL_API_KEY` for internal task creation/read operations in staging and production. Keep all keys in deployment secrets, not in Git.

## Key Configuration
- `REVIEW_BUCKET`
- `REVIEW_PROVIDER`: `auto`, `local_queue`, or `label_studio`
- `REVIEW_REQUEST_TIMEOUT_SECONDS`
- `REVIEW_LABEL_STUDIO_TIMEOUT_SECONDS`
- `REVIEW_MAX_INFLIGHT_REQUESTS`
- `REVIEW_HIGH_PRIORITY_CONFIDENCE_THRESHOLD`
- `REVIEW_FALLBACK_TO_LOCAL_QUEUE`
- `REVIEW_PERSIST_TASKS`
- `REVIEW_DECISION_API_KEY`
- `REVIEW_DECISION_API_KEYS`
- `REVIEW_POLL_INTERVAL_SECONDS`
- `REVIEW_WAIT_TIMEOUT_SECONDS`
- `IDP_REQUIRE_INTERNAL_AUTH`
- `IDP_INTERNAL_API_KEY`
- `LABEL_STUDIO_URL`
- `LABEL_STUDIO_TOKEN`
- `LABEL_STUDIO_PROJECT_ID`
- `LABEL_STUDIO_AUTH_SCHEME`: `token`, `pat`, `bearer`, or `auto`
- `LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN`
- `LABEL_STUDIO_USERNAME`
- `LABEL_STUDIO_PASSWORD`
- `LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK`

## Local Development Behavior
With the default `.env`, `LABEL_STUDIO_TOKEN` is empty. The service therefore uses `LocalQueueProvider` and persists tasks to `review-artifacts`.

```env
REVIEW_PROVIDER=auto
LABEL_STUDIO_TOKEN=
```

This is the recommended local path because review task creation works even before `Label Studio` authentication is configured.

## Label Studio Behavior
Bootstrap the initial Label Studio account before logging in:

```env
REVIEW_PROVIDER=label_studio
LABEL_STUDIO_URL=http://label-studio:8080
LABEL_STUDIO_TOKEN=<label-studio-api-token>
LABEL_STUDIO_PROJECT_ID=1
LABEL_STUDIO_USERNAME=<your-email-address>
LABEL_STUDIO_PASSWORD=<strong-password-between-8-and-128-characters>
LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK=true
LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN=true
LABEL_STUDIO_AUTH_SCHEME=token
```

Compose passes `LABEL_STUDIO_TOKEN` into Label Studio as `LABEL_STUDIO_USER_TOKEN`, keeping the bootstrapped user's API token aligned with `human-review-console`.
Because `LABEL_STUDIO_USER_TOKEN` is a legacy token, the Label Studio container
also enables `LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN=true`. For a personal access
token, set `LABEL_STUDIO_AUTH_SCHEME=pat`; the integration exchanges it for a
short-lived bearer access token. Use `bearer` only when supplying an access
token directly.

Recreate Label Studio after setting the account details:

```bash
docker compose up -d --force-recreate label-studio
```

Then open `http://localhost:8080` and log in using `LABEL_STUDIO_USERNAME` and `LABEL_STUDIO_PASSWORD`.

### Project Name Versus Project ID

The Label Studio project display name and `LABEL_STUDIO_PROJECT_ID` are different values.

For example:

```text
Project name: IntelligentDP #1
Project ID:   1
```

Naming a project `IntelligentDP #1` does not automatically make its numeric project ID equal to `1`. Label Studio assigns the project ID internally when the project is created.

Open the project in Label Studio and inspect its browser URL:

```text
http://localhost:8080/projects/1/data
```

The number immediately after `/projects/` is the value to use:

```env
LABEL_STUDIO_PROJECT_ID=1
```

The project ID can also be verified through the Label Studio API:

```bash
curl \
  -H "Authorization: Token ${LABEL_STUDIO_TOKEN}" \
  http://localhost:8080/api/projects
```

Find the response object whose `title` is `IntelligentDP #1`, then use its numeric `id` value for `LABEL_STUDIO_PROJECT_ID`.

After confirming the project ID, recreate the integration service so it reloads the token and project settings:

```bash
docker compose up -d --force-recreate human-review-console
```

The username is the account email and cannot be changed later from Label Studio account settings. Do not commit real passwords or tokens.

If Label Studio returns an error and fallback is enabled, the service queues the task locally and stores the provider failure in the persisted review artifact.

### Label Studio Access Points

```text
Label Studio reviewer UI: http://localhost:8080
Human review API docs:    http://localhost:8016/docs
```

Label Studio is the reviewer-facing UI. `human-review-console` remains the API and integration layer responsible for task creation, idempotency, priority, persistence, and provider fallback.

For review-to-delivery, configure the Label Studio project with a Choices control named `decision` containing `Approve` and `Reject`. Add a TextArea control named `corrected_fields_json` for a JSON object of corrected fields, for example `{"invoice_number":"INV-42","total_amount":"42.00"}`. An annotation without an explicit recognized decision remains pending; an approval without corrected fields also remains pending. A rejected annotation terminates the workflow without delivery. The project must retain the imported `job_id`, `tenant_id`, and `review_task_id` task data so the decision can be matched to the original job.
The Label Studio import response must include a task ID. An import without one is treated as a provider error rather than a queued review that could never be polled.

### Verify Label Studio Integration

Check that both services are running:

```bash
docker compose ps label-studio human-review-console
```

Confirm which review provider the integration service selected:

```bash
curl http://localhost:8016/health
```

Expected response when the token is loaded:

```json
{
  "status": "ok",
  "provider": "label_studio"
}
```

Verify actual authentication and access to the configured project:

```bash
curl -fsS http://localhost:8016/health/provider
```

Expected response includes:

```json
{
  "status": "ok",
  "provider": "label_studio",
  "project_id": 1,
  "project_title": "IntelligentDP #1",
  "auth_mode": "token"
}
```

Provider health returns HTTP `503` for an invalid token, unavailable Label
Studio instance, or inaccessible project. Staging deployment retries this
check and fails rather than silently accepting local-queue fallback.
The staging overlay defaults `STAGING_REVIEW_PROVIDER=label_studio`, making
Label Studio configuration mandatory for deployment verification.

Create a test review task:

```bash
curl -X POST http://localhost:8016/review/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "label-studio-smoke-1",
    "reasons": ["low_confidence:0.720"],
    "fields": {
      "invoice_number": "INV-001"
    },
    "confidence": 0.72,
    "route": "invoice",
    "verdict": "needs_review"
  }'
```

Successful Label Studio task creation returns:

```json
{
  "review_status": "queued_in_label_studio",
  "provider": "label_studio"
}
```

If the response contains `queued_without_label_studio` and `local_queue`, inspect the `human-review-console` logs:

```bash
docker compose logs --tail=200 human-review-console
```

Common causes are an incorrect project ID, expired or incorrect token, missing project access, or Label Studio being unavailable.

## Data and Storage
- Primary artifact bucket: `REVIEW_BUCKET`.
- Fallback artifact bucket, if primary write fails: `VALIDATION_BUCKET`.
- Artifact key pattern:
```text
jobs/<job_id>/review/<review_task_id>.json
```

## Failure Handling
- Provider failure falls back to local queue when `REVIEW_FALLBACK_TO_LOCAL_QUEUE=true`.

- Provider failure returns HTTP `502` when fallback is disabled.

- Busy worker pool returns HTTP `503` so Temporal can retry.

- Request timeout returns HTTP `502` or local fallback depending on fallback configuration.

- Task persistence falls back from `REVIEW_BUCKET` to `VALIDATION_BUCKET`.


## Security and Compliance
- Task artifacts include reasons, extracted fields, source artifact pointers, provider reference, and provider response for traceability.

- Logs avoid dumping full extracted fields.

- Label Studio access uses token-based API authentication.

- Deterministic review IDs help prevent duplicate tasks from workflow retries.


## Observability
- FastAPI app is instrumented with the shared Prometheus middleware.
- `review_status`, `provider`, `priority`, and reason codes can be aggregated for review queue dashboards.


## How To Run

Run with Docker Compose service:
```bash
docker compose up -d human-review-console
```

Run locally from repository root:
```bash
uv sync --frozen --no-default-groups --group base
PYTHONPATH=. .venv/bin/uvicorn human_review_console.app.main:app --host 0.0.0.0 --port 8016 --reload
```

Run tests:
```bash
.venv/bin/python -m unittest discover -s human_review_console/tests -p 'test_*.py' -v
```

Run the focused workflow contract tests:
```bash
.venv/bin/python -m unittest workflow_orchestrator.tests.test_pipeline -v
```

## Non-Goals
- Replacing Label Studio's annotation UI.
- Final delivery approval workflow.
- Training data export automation.

## Change Log
- 2026-05-21: Documentation sync update to reflect current service naming and cross-service ingestion contract expectations.
- 2026-06-03: Implemented modular provider-based review task creation, local queue fallback, Label Studio integration, deterministic task IDs, artifact persistence, status lookup, tests, and run instructions.
- 2026-07-01: Added Label Studio provider readiness checks, legacy/PAT/bearer
  authentication modes, deployment-time project verification, and explicit
  legacy-token enablement for the bootstrapped `LABEL_STUDIO_USER_TOKEN`.
- 2026-06-04: Added first-user Label Studio bootstrap configuration using username, password, and the existing integration access token.
- 2026-06-04: Documented project-name versus numeric project-ID behavior, project-ID lookup, service access points, and end-to-end Label Studio integration verification.

## 2026-09-30: Complete Reviews And Resume Delivery

Creating a review task no longer finishes the Temporal workflow. It remains in `PENDING_HUMAN_REVIEW`, with a readable interim result, until this service records a decision. The worker polls `GET /review/tasks/{job_id}/{review_task_id}/decision?tenant_id=...` using `REVIEW_DECISION_API_KEY` in `X-IDP-Review-Key`. The default poll interval is 60 seconds and the default wait is seven days; set `INGESTION_WORKFLOW_TIMEOUT_MINUTES` at least one hour longer.

For Label Studio tasks, the poll endpoint reads the configured project's task and latest completed annotation. Configure the Label Studio project with a single-choice `decision` control whose values are `Approve` or `Reject`, plus a `corrected_fields_json` TextArea. The text area should contain a JSON object of corrected fields, for example `{"invoice_number":"INV-42","total_amount":"42.00"}`. Individual TextArea controls named after field keys also work. A submitted approval without corrected fields remains pending; the reviewer must provide at least one corrected field. Corrected fields override the machine values, and validation checks required fields and formats before delivery. Rejection does not deliver.

Local queue decisions can be submitted by a trusted reviewer client. Configure `REVIEW_DECISION_API_KEYS` as comma-separated `key:tenant-id` entries, one tenant-scoped review credential per ingestion tenant. Keep this separate from `REVIEW_DECISION_API_KEY`, which authorizes the Temporal worker's decision polling. Example for a local default-tenant review:

```bash
curl -X POST http://localhost:8016/review/tasks/<job_id>/<review_task_id>/decision \
  -H "X-IDP-Review-Key: <default-tenant-review-key>" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"default","reviewer_id":"reviewer-1","decision":"approved","fields":{"invoice_number":"INV-42","invoice_date":"2026-09-30","total_amount":"42.00"}}'
```

`decision` can also be `rejected`, with an empty `fields` object. Identical submissions are idempotent; a conflicting second decision returns HTTP 409. Review decisions are stored in Postgres with a unique `(tenant_id, job_id, review_task_id)` key and mirrored to `REVIEW_BUCKET` for audit. If the object store mirror fails, the Postgres decision remains authoritative. The reviewer key is accepted only for its configured tenant; the task's stored tenant and Label Studio task identity are checked before any decision is returned.

In staging/production, configure `IDP_REQUIRE_INTERNAL_AUTH=true`, a strong `IDP_INTERNAL_API_KEY`, strong `REVIEW_DECISION_API_KEY`, and `REVIEW_DECISION_API_KEYS` covering every tenant. Update the GitHub `STAGING_ENV_FILE` secret before deploying this release. The staging Compose overlay does not expose the review API on a host port; use a trusted internal client or a protected operator tunnel for manual decisions. No Label Studio webhook configuration is needed because the worker polls task annotations.

### Authenticated Label Studio event hint

`POST /review/tasks/{job_id}/{review_task_id}/label-studio-event?tenant_id=...`
requires `X-IDP-Review-Key` mapped to that tenant in `REVIEW_DECISION_API_KEYS`.
The endpoint ignores submitted annotation fields and re-fetches the registered
external task with the service's Label Studio credential. Project, tenant, job,
and task identities must match before persisting a decision. Configure this
per-task URL in a trusted integration adapter; this is not a project-wide webhook
payload dispatcher. `REVIEW_DECISION_API_KEY` must also be configured for the
existing authoritative lookup path. Keep the endpoint behind encrypted transport.

Missing reviewer identity leaves the task pending; `completed_by` may be a scalar
ID or an object containing `id`. No synthetic reviewer identity is supplied.
Existing immutable decision persistence makes repeat notifications return the
stored decision. Temporal observes it through its existing polling loop, not an
immediate signal. Annotation edits after a committed decision do not revise it.

Focused tests cover tenant-key denial, authoritative lookup rather than trusting
event fields, and missing/nested reviewer identities. The live drill below verifies
decision integration, persisted task replay and Temporal history replay.
Project-wide webhook registration and concurrent provider-import reconciliation
remain separate from this per-task integration.

### Durable OCR exceptions

`POST /review/ocr-exceptions` accepts tenant, job, and typed `failure_code` using
`X-IDP-Review-Key` with the worker credential. Records use the authoritative
Postgres `review_decisions` table under reserved task ID `ocr-exception`; database
uniqueness preserves the first exception across retries and restarts. Retrieve
with `GET /review/ocr-exceptions/{job_id}?tenant_id=...` using that tenant's review
key. This is an exception lookup API; an operator resolution/requeue UI remains
future work. Provision the worker key before enabling this path.

Live persistence verification was performed against an isolated Postgres 16
container (`idp-verification-postgres`, host port 15439). Both corrected decisions
and exception records survived a database restart, duplicate writes preserved
the first result, and cross-tenant lookups returned no record. This exercises
committed database restart recovery; it does not simulate a crash during commit.
Run `python -m scripts.verify_review_persistence` with the target test database
settings. The script writes only tenant `verification`, job `recovery-test`.

### Completed live decision and recovery checks

The live verification profile (`infra/verification/README.md`) now exercises this
service against Label Studio 1.20.0 and real Postgres. Approved corrected fields
were revalidated and delivered with the Label Studio reviewer ID after a worker
SIGKILL/restart. Rejection, invalid corrections and timeout produced no delivery.
Duplicate event calls returned the committed decision; duplicate task creation
reused its persisted external task reference. Imports now explicitly request
`return_task_ids=true`. The fast retry path requires the persisted task record;
concurrent imports or a crash before that record commits still require provider
creation reconciliation before claiming exactly-once task creation.

## 2026-10-01: Tenant Boundary Verification

This service's processing endpoint is included in the live intake/authorization
acceptance gate. A call without its internal service credential returns `401`;
authorized calls referring to another tenant/job's artifacts return `403` before
processing those objects. Review uses its dedicated tenant/worker credentials
for decisions and also rejects an operator key bound to another tenant.

Run `bash scripts/run_ingestion_verification.sh` from the repository root. Inspect
`artifacts/ingestion-recovery/tenant-boundaries.json`. The profile is isolated,
uses local fixture credentials, and leaves its services running after the test.
Keep processing APIs private: a shared internal key authenticates a trusted
service, not an end-user tenant principal. Public tenant access is mediated by
ingestion. See `infra/architecture/IMPLEMENTATION_ORDER.md` for the next exception,
delivery, page-execution, and representative-quality milestones; this gate does
not mark those implementations complete.
