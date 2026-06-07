# Human Review Console

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
- `POST /review/tasks`
- `GET /review/tasks/{job_id}/{review_task_id}`

## Key Configuration
- `REVIEW_BUCKET`
- `REVIEW_PROVIDER`: `auto`, `local_queue`, or `label_studio`
- `REVIEW_REQUEST_TIMEOUT_SECONDS`
- `REVIEW_LABEL_STUDIO_TIMEOUT_SECONDS`
- `REVIEW_MAX_INFLIGHT_REQUESTS`
- `REVIEW_HIGH_PRIORITY_CONFIDENCE_THRESHOLD`
- `REVIEW_FALLBACK_TO_LOCAL_QUEUE`
- `REVIEW_PERSIST_TASKS`
- `LABEL_STUDIO_URL`
- `LABEL_STUDIO_TOKEN`
- `LABEL_STUDIO_PROJECT_ID`
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
```

Compose passes `LABEL_STUDIO_TOKEN` into Label Studio as `LABEL_STUDIO_USER_TOKEN`, keeping the bootstrapped user's API token aligned with `human-review-console`.

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
- 2026-06-04: Added first-user Label Studio bootstrap configuration using username, password, and the existing integration access token.
- 2026-06-04: Documented project-name versus numeric project-ID behavior, project-ID lookup, service access points, and end-to-end Label Studio integration verification.
