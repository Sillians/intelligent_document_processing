# Implementation and verification status

This inventory distinguishes local implementation from deployed verification.
Source-mounted code has live functional evidence in the isolated verification
stack. These changes have not been promoted as a tested production image. The
ordered remaining milestones are in `IMPLEMENTATION_ORDER.md`.

| Requirement | Current local implementation | Remaining acceptance work |
| --- | --- | --- |
| Human review | Durable decisions, authoritative authenticated Label Studio events, corrected revalidation/delivery, rejection/timeout, worker restart and history replay verified live | Concurrent external task creation/reconciliation, project-wide webhook setup, older-history compatibility |
| OCR failure | Typed failures, durable tenant exceptions, real validated secondary Tesseract; scoped operator resolution, linked reprocessing and commit-crash recovery verified live | Intended production-engine quality evidence, deployed migration and failure/load tests |
| Recoverable intake | Transactional audits/outbox, fenced leases, verified upload recovery, durable key aliases, concurrent deduplication; five live process-crash windows passed | Actual staging schema rollout, promoted image verification, sustained load/outage tests |
| Tenant scope | Credential-derived tenant identity, scoped keys, private-stage authentication; live denial at all eight processing endpoints and scoped intake reads | Production network/transport controls, service identity granularity, credential rotation |
| Page model | Independent OCR/layout activities and validated aggregation; bounded PDF/TIFF rendering, native-text artifacts and total pixel budget; live page retry/restart-to-delivery gate passed | Independently checkpointed rendering, native-text preference and full memory sandbox |
| Versioned contracts | Versioned page dictionaries and page validation | Typed schemas for all stage outputs, provenance, coordinates, model versions and failures |
| Quality | Per-family/positive-field diagnostics, document reliability bins, explicit validation labels, latency/label coverage and approved-manifest preflight | Permissioned held-out corpus, real baseline, negative-field/table scoring, fitted calibration, value evidence and reviewed policy |
| Delivery | PostgreSQL ledger, frozen envelopes, fenced attempts/leases, bounded retries, dead-letter redrive, acknowledged-only Temporal completion; live crash/restart tests | Legacy receipt migration, deployed image/restore evidence, destination-owned deduplication and late-delivery SLA |
| Operations | Existing deployment and observability configuration | Live recovery, restore and rollback evidence; persistent separate environments; verified image and approval controls |
| Service boundaries | Provisional assessment from measured stage timings | Representative measurements and transport/queue overhead before consolidation |

## Historical Benchmark Blockers

The following records describe the earlier inspection, not a claim that the
engine still lacks an IDP stack. Later live evidence below resolves that blocker.

- Deployed release marker references `fb4b9d164e0d3902d5283485f6d180a0322ac7c6`;
  new benchmark code is uncommitted locally.
- No representative JSONL manifest was found in the staging directory. The
  repository's single synthetic sample is not sufficient.
- GitHub CLI is unauthenticated. The connected GitHub integration can read the
  repository, but no environment-variable or new-workflow-dispatch operation was
  available through that integration.
- The active OrbStack Docker context contains no IDP staging containers. Other
  projects' containers were left unchanged.

Do not mark Benchmark Staging passed or set the manifest variable to a fabricated
path. Obtain the labeled corpus, validate it, deploy the code, configure the
staging environment variable, and dispatch the workflow using the instructions
in `.github/README.md`. Keep permission records and benchmark artifacts under
appropriate access controls; extracted document content can be sensitive.

## Live milestone evidence (2026-09-30)

The earlier absence of an IDP stack has been resolved by
`infra/verification/compose.yml` on the `idp-staging` context. Host and service
Tesseract are installed. The live drill verifies worker SIGKILL/restart,
Label Studio approval through revalidation and object-storage delivery,
rejection/invalid-correction/timeout without delivery, repeated decision/task/
delivery requests, real secondary OCR acceptance/rejection, and persistent
exception routing after three failed OCR attempts. Exported histories are replayed
using the production workflow class. Evidence is recorded in
`artifacts/live-milestone/report.json`; rerun with `scripts/run_live_milestone.sh`.
These resolve the earlier live-test blockers for this functional milestone.
Broader production items in the inventory, including concurrent external side
effects, older-history migrations, and representative quality remain separate.

## Intake and Tenant Gate Evidence (2026-10-01)

`scripts/run_ingestion_verification.sh` exercises production repository/storage/
Temporal registration calls in child processes terminated at five crash windows.
It verifies transactional lifecycle audits, checksum rejection, lease fencing,
and replay after a lost Temporal-start acknowledgement. Separate live HTTP checks
verify durable dedupe aliases, changed-content 409, concurrent equal-content
registration, independent tenant jobs for equal content/keys, scoped intake
reads, and missing credentials/foreign object references at all eight stages.

Evidence is in `artifacts/ingestion-recovery/report.json` and
`artifacts/ingestion-recovery/tenant-boundaries.json`. These are functional fixture
checks, not representative quality or production promotion evidence. The next
implementation pair was operator exception resolution/reprocessing and durable
per-destination delivery; the separate evidence below closes its functional gate.

## Exception Recovery And Delivery Evidence (2026-10-01)

`scripts/run_recovery_delivery_verification.sh` passed all 12 check groups in
`artifacts/recovery-delivery/report.json`. Three real OCR-outage exceptions are
created through public intake. Two linked replacements complete actual Label
Studio approval, corrected validation and acknowledged delivery; their histories
replay. One resolution is recovered after process termination immediately after
commit. Corrupt source stays unresolved, and tenant/key conflicts are denied.

Delivery processes terminate before send, after signed receiver acceptance, and
after receipt PUT. Frozen bytes and current lease fencing reconcile safely;
receiver-side PostgreSQL deduplication applies one effect even on a lost-ack
resend. Partial failure dead-letters, operator redrive touches only failed work,
concurrent requests do not replay acknowledged providers, and the service drains
committed work after restart without caller retry. Metrics and alert rule tests
cover dead letters and unfinished delivery age. This is source-mounted functional
evidence, not quality-policy approval or production promotion. The next pair is
independent page execution and representative measurement.

## Page Execution Evidence (2026-10-03)

`scripts/verify_page_execution.py` passed on the isolated source-mounted stack:
two real OCR/layout pages aggregate in order, a controlled retryable page-two
failure survives a graceful worker restart without repeating page one, and real
Label Studio correction completes revalidation and acknowledged delivery.
OCR activity attempt counts were `{1: 1, 2: 2}`. Evidence is recorded in
`artifacts/page-execution/report.json`. This is not a SIGKILL or representative
accuracy test. Evaluation tracking was unavailable in this reduced profile.
Batch replay passed 18 old and new histories with clean process exit; see
`artifacts/page-execution/replay.json`.

Measurement now reports per-family/annotated-positive-field scores, reliability
bins, explicit validation-label coverage and latency coverage. Approved policies
require reviewed, non-synthetic, permission-declared manifest evidence before
submission. A permissioned held-out corpus and approved production-quality policy
remain unavailable; synthetic fixtures do not close that acceptance gate.
