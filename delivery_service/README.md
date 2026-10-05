# Delivery Service

## Core Purpose
Publish approved results and signed workflow events using an authoritative
PostgreSQL delivery ledger. A background reconciler, not the caller, owns retry
and recovery. This revision supersedes the earlier S3-only receipt/in-process
retry design. Human-approved results already resume through validation and delivery.

## Run
From the repository root, configure the database, S3 and internal credentials:

```bash
docker compose up -d postgres seaweedfs delivery-service
# Or run Python against reachable configured dependencies:
uv sync --frozen --no-default-groups --group base
PYTHONPATH=. .venv/bin/uvicorn delivery_service.app.main:app --host 127.0.0.1 --port 8017
.venv/bin/python -m unittest delivery_service.tests.test_main -v
```

`/health` is liveness; `/ready` requires the ledger. Compose now checks `/ready`
and waits for PostgreSQL. Ledger creation failure prevents startup; there is no
volatile success fallback. Local startup creates additive tables with `IF NOT
EXISTS`. Production must roll out these schemas under a controlled migration,
back up the database, and restore-test before promotion.

## Persistence And Recovery
- `delivery_operations`: immutable request fingerprint, redacted request, frozen envelope, configured destination and tenant/job identity.
- `delivery_work`: one row per destination plus a separate `__receipt__` projection; due time, bounded attempt budget, lease token and expiry.
- `delivery_attempts`: immutable attempt starts/outcomes, including `unknown` when an abandoned lease expires.
- `delivery_operator_audit`: authenticated actor and reason for redrive, retained independently of operation cleanup.

Registration and work creation commit before any external effect. Claims use
`FOR UPDATE SKIP LOCKED`; acknowledgements/failures require the current unexpired
lease token. A restart recovers due work without another client request. Attempt
budgets include crashes, not just explicit provider errors. HTTP errors other
than 408, 425 and 429 in the 4xx range are permanent; transient failures use
bounded backoff. Exhaustion enters `dead_letter`.

Object storage and webhook providers implement the modular
`DeliveryProvider.deliver(DeliveryContext)` interface in `app/providers.py`.
Acknowledged destinations never resend because another destination or receipt
projection failed. The S3 receipt is projected only after all destinations are
acknowledged and is itself retryable durable work. A failed receipt PUT cannot
be mistaken for successful delivery. Deterministic tenant/job keys make repeated
object PUTs replace the same object after an acknowledgement-loss crash.

Remote webhooks are **at-least-once**, not exactly-once. The receiver must atomically
persist the stable `Idempotency-Key` with its own side effect. The stored canonical
envelope and URL stay fixed across retries; HMAC uses the current configured
signing secret. Coordinate signing-key rotation with receivers. HTTPS, egress
policy and destination authorization are deployment requirements; an allowlisted
hostname alone is not a complete SSRF/network isolation policy.

## APIs
Private calls require `X-IDP-Internal-Key` when internal authorization is enabled.
Tenant/job identities and artifact paths are checked at service boundaries.

| Endpoint | Behavior |
| --- | --- |
| `POST /deliver` | Approved structured payload; 200 only after every destination and receipt is acknowledged, otherwise 202 with pending/dead-letter work. |
| `GET /deliveries/{job_id}/{delivery_id}?tenant_id=...` | Tenant/job-scoped authoritative operation status/receipt; 404 outside scope. |
| `POST /webhooks/events` | Durable signed event; unset URL yields a durably recorded skipped event. |
| `GET /delivery-queue` | Tenant-scoped unfinished work; bounded `limit`/`offset`; requires operator and internal keys. |
| `POST /delivery-queue/{operation_id}/redrive` | Retry only dead-letter destinations; reason required; authenticated audit; 202. |
| `GET /health`, `GET /ready`, `GET /metrics` | Liveness, dependency readiness and telemetry. |

The request body includes `tenant_id`, `job_id`, `payload`, `approval_status`,
optional `destinations`, `idempotency_key`, `webhook_url`, source artifact keys,
and `metadata`. Default allowed approvals are `auto_approved,human_approved`.
An internal approval string is not independent proof of authorization; keep
this API private and route document approval through validation/review.

An explicit idempotency key fixes the operation ID. Without one, the semantic
request determines it. The committed fingerprint includes the request, providers
and resolved URL. Another job, payload, approval, source, metadata or destination
under that ID returns 409. Replays cannot replace the frozen envelope. Changing
default provider/URL configuration can therefore conflict with an old caller
replay; already committed background work still uses its original destination.
Non-finite JSON values are rejected.

Existing S3-only receipts without ledger rows fail closed with 409 rather than
resend uncertain historical deliveries, including the previous generated ID
format. Storage lookup outages return 503, not 'receipt absent'. Before rollout,
export and verify legacy receipts, migrate their acknowledged destinations into
the ledger through a reviewed migration, or drain old workflows first. An
automatic historical receipt importer is not included. Never solve a 409 by
changing the key and sending the same historical result again.

Example operator recovery from an authorized private network:

```bash
curl -fsS "$DELIVERY_URL/delivery-queue" \
  -H "X-IDP-Internal-Key: $IDP_INTERNAL_API_KEY" \
  -H "X-IDP-Operator-Key: $OPERATOR_KEY"
curl -fsS -X POST "$DELIVERY_URL/delivery-queue/$OPERATION_ID/redrive" \
  -H "X-IDP-Internal-Key: $IDP_INTERNAL_API_KEY" \
  -H "X-IDP-Operator-Key: $OPERATOR_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"Destination repaired and verified"}'
```

A non-dead-letter operation returns 409; another tenant receives 404. Redrive
resets the failed destination's budget but preserves lifetime attempts and all
successful work. It does not mutate payload, approval or URL.

## Configuration
| Variable | Default / purpose |
| --- | --- |
| `POSTGRES_*` | Mandatory shared ledger connection. |
| `DELIVERY_PROVIDERS` | `object_storage`; optional `webhook`. |
| `DELIVERY_BUCKET` | `delivery-artifacts`; receipt storage may fall back to validation bucket. |
| `DELIVERY_REQUIRE_APPROVAL` | `true`. |
| `DELIVERY_ALLOWED_APPROVAL_STATUSES` | `auto_approved,human_approved`. |
| `DELIVERY_DURABLE_MAX_ATTEMPTS` | `10` claims per destination per redrive cycle, including abandoned claims. |
| `DELIVERY_RECONCILE_INTERVAL_SECONDS` | `5`. |
| `DELIVERY_LEASE_SECONDS` | `120`; size above maximum outbound timeout and expected execution time. |
| `DELIVERY_RETRY_BACKOFF_SECONDS` | `0.5`; exponential backoff capped at 300 seconds. |
| `DELIVERY_REQUEST_TIMEOUT_SECONDS` | `60`; processing timeout. |
| `DELIVERY_MAX_INFLIGHT_REQUESTS` | `16`; request capacity. |
| `DELIVERY_WAIT_TIMEOUT_SECONDS` | `43200`; Temporal waits up to 12 hours for acknowledgement. |
| `DELIVERY_POLL_INTERVAL_SECONDS` | `10`; durable Temporal polling. |
| `DELIVERY_REDACT_FIELDS` | Comma-separated names recursively redacted in payload, metadata and event data before storage/send. |
| `DELIVERY_PAYLOAD_FORMAT_VERSION` | `1.0`. |
| `DELIVERY_WEBHOOK_URL`, `DELIVERY_WEBHOOK_SECRET` | Optional fixed URL and signing secret. |
| `DELIVERY_WEBHOOK_REQUIRE_SIGNATURE` | `true`. |
| `DELIVERY_ALLOW_REQUEST_WEBHOOK_URL` | `false`; enable only with approved `DELIVERY_WEBHOOK_ALLOWED_HOSTS`. |
| `DELIVERY_WEBHOOK_TIMEOUT_SECONDS` | `15`. |
| `IDP_OPERATOR_API_KEYS` | Separate comma-separated `key:tenant` mappings; never reuse public intake keys. |

`DELIVERY_RETRY_MAX` is a legacy setting and no longer controls provider retries.
Use `DELIVERY_DURABLE_MAX_ATTEMPTS`. Payloads and reason text may be sensitive;
restrict database/object access, encryption, backups and operator evidence.

## Workflow And Retention
New workflows use the `durable-delivery-v1` Temporal patch. They report
`pending_delivery` or `delivery_exception`, poll rather than treating 202 as
success, and finish `delivered` only after acknowledgement. Dead letters can be
redriven while the workflow waits; expiration returns `delivery_timed_out`.
The delivery queue is not automatically cancelled at that workflow deadline,
so a later acknowledgement is still possible. Set an operational SLA and
reconcile timed-out workflows before redrive; deadline is not proof of no effect.

Retention protects jobs with active pending/leased delivery (but not operations
already dead-lettered), and active reprocessing parents/children. Dead letters
follow configured derived retention; resolve them before that deadline. Expired
operations cascade attempts/work cleanup, while operator audit uses audit TTL.
`enforce_retention.py --apply` takes exclusive maintenance locks (including reads)
while deleting to avoid claim/register races; plan a
maintenance window and run its dry run first. Backups are still required.

## Monitoring And Verification (2026-10-01)
Metrics: `idp_delivery_pending_destinations`,
`idp_delivery_dead_letter_destinations`,
`idp_delivery_oldest_unfinished_age_seconds`. Prometheus adds
`IDPDeliveryDeadLetters` (>0 for 2 minutes) and `IDPDeliveryAgeHigh` (>15 minutes
for 5 minutes), with rule tests. Reconciliation errors are logged without raw
request credentials; durable failure text is categorized rather than a raw URL.

```bash
bash scripts/run_recovery_delivery_verification.sh
```

The isolated source-mounted profile checks crashes before send, after signed
receiver acceptance, and after receipt PUT, concurrent requests, stale leases,
bounded failures, tenant-denied redrive, preservation of acknowledged providers,
immutable replay conflicts, and service restart without caller retry. A real
receiver atomically deduplicates the delivery key in PostgreSQL; lost-ack replay
sends identical bytes twice but applies one effect. Results:
`artifacts/recovery-delivery/report.json`. This is functional fixture evidence,
not representative accuracy evidence, a deployed image promotion or a claim
that arbitrary webhook receivers provide exactly-once effects.
