# Ordered Implementation Loops

Deliver each pair with functional evidence before moving to the next pair.
Synthetic documents can exercise state transitions, but cannot approve a
production-quality policy. Local source-mounted verification is not an image
promotion or a GitHub Actions run.

## 1. Recoverable Intake and Tenant Authorization

**Submit -> persist -> dispatch -> reconcile** owns registration, verified raw
artifacts, transactional dispatch records, fenced leases, stable Temporal IDs,
idempotency request bindings, and committed lifecycle audits.

**Authorize -> scope -> deny** derives public tenant identity from credentials,
rejects tenant-header impersonation, scopes status/audit/results and objects,
and requires internal authorization on private stage APIs.

Implemented and live-verified in the isolated profile on 2026-10-01. Acceptance:
five terminated-process crash windows recover safely; stale dispatch workers
cannot mutate a newer claim; same-key/different-content returns 409; concurrent
same-content intake registers one job; every stage denies foreign artifacts.
Rerun `bash scripts/run_ingestion_verification.sh`. Inspect both reports under
`artifacts/ingestion-recovery/` and the service README for failure semantics.

Remaining production acceptance includes schema rollout on actual staging,
tested promoted image SHA, gateway/body limits, protected network transport,
credential rotation, backup/restore, and sustained failure/load testing.

## 2. Exception Resolution and Durable Delivery

**Fail -> resolve -> reprocess** must build on existing typed OCR failures and
durable exception records, not convert an outage into text. Add a tenant-scoped
operator queue, immutable resolution actor/reason, and an idempotent request
linking an original failed execution to a new processing generation. Use intake's
durable dispatch mechanism and verified source artifact. Preserve the original
failure, prevent parallel accidental reprocessing, revalidate the new result,
and deny cross-tenant inspection or resolution.

Acceptance: exhausted transient retries produce a visible exception; authorized
resolution survives process restart; replay creates one replacement execution;
unavailable/corrupt source artifacts stay unresolved; reject and timeout paths
never deliver; recovery works with real primary or validated secondary OCR.

**Deliver -> acknowledge -> retry -> reconcile** must persist the immutable
delivery envelope, request fingerprint, per-destination attempts, leases, due
times, and acknowledged receipts before depending on caller retries. Add bounded
durable retry, dead-letter inspection/redrive, reconciliation, and queue-age
alerts. Successfully acknowledged destinations must not be resent when another
destination fails. Reusing a key with another envelope must return 409.

Acceptance: crash before send, after remote acceptance, before acknowledgement,
and between destination sends; restart with no caller; reconcile expired leases;
avoid replaying acknowledged providers; tenant-scope receipts and dead letters.
Remote webhooks are at-least-once: receivers must persist and deduplicate the
delivery/event key atomically with their own side effect. Do not claim exactly-once
remote delivery when an acknowledgement can be lost.

Implemented and live-verified in the isolated profile on 2026-10-01. Run
`bash scripts/run_recovery_delivery_verification.sh`; all 12 check groups in
`artifacts/recovery-delivery/report.json` must pass. Actual intake during an OCR
outage, scoped operator reprocessing, resolution-commit crash recovery, real
Label Studio approval/revalidation/delivery and two new history replays pass.
Signed receiver tests cover before-send, lost-ack and receipt-PUT process crashes,
unknown attempt recovery, stale-lease fencing, concurrent calls, bounded dead
letters, failed-only redrive and delivery service restart without caller retry.

Remaining production acceptance includes controlled schema migration, handling
legacy S3-only receipts without duplicate effects, end-to-end deployed-image
verification, receiver-side idempotency, secured operator access, retention SLA,
late-delivery reconciliation and load/restore tests. The verification receiver
demonstrates one atomically deduplicated effect; arbitrary receivers do not
inherit that guarantee. Continue with milestone 3, not production-quality claims.

## 3. Page Execution and Representative Quality

**Render -> process pages -> aggregate** adds bounded rendering, preserved native
PDF text/provenance, shared typed page/token/zone contracts, deterministic page
IDs, per-page Temporal activities and retry, and ordered document aggregation.
Enforce file-type, page, dimensions, total decoded pixels, and memory budgets.

Acceptance: multi-page PDF/TIFF, native/scanned/mixed PDFs, stable coordinates,
one failed page retried without rerunning successful pages, reordered/duplicate
page manifests denied, corruption/oversized input rejected, complete aggregation.

**Measure -> diagnose -> improve** requires a permissioned representative held-out
gold set for actual document families, with explicit field, OCR-text, page-count,
and validation-decision labels. Score type/field precision and recall, CER/WER,
validation accuracy, throughput, review rate, and adequately sampled p95 latency.
Calibrate confidence against observed errors; link each value to source evidence.
Missing labels must not become invented scores. Compare changes against fixed
acceptance thresholds, then use measured compute/transport/queue overhead to
decide whether a stage deserves a separately deployed HTTP service.

Status (2026-10-03): independent OCR/layout activities, stable per-page IDs,
page-local artifacts, validated ordered aggregation, preserved bounded native PDF
text and total decoded-pixel admission are implemented. Rendering/preprocessing
still runs as one bounded document activity. The controlled live drill in
`scripts/verify_page_execution.py` checks a transient page-two failure and worker
restart without rerunning successful page one; inspect its report before claiming
that gate passed. Old histories retain their branch through a Temporal marker.

Measurement now reports per-family/per-field diagnostics and document-level
confidence reliability. Validation accuracy requires explicit labels; family F1
and validation/latency coverage cannot hide behind an overall mean. Approved runs
preflight reviewed real-document manifests. See `infra/quality/README.md`.
No permissioned representative corpus/baseline has been supplied. Native-text
preference, independently checkpointed rendering, complete typed/evidence-backed
field contracts, fitted confidence calibration and production-quality acceptance
remain work. Synthetic success cannot substitute for that evidence.
