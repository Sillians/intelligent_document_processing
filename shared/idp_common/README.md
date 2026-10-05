# Shared IDP Contracts (2026-09-30)

## Page Execution Contract (2026-10-03)
Ordered page manifests now also reject duplicate image artifact keys. Independent
stage artifacts use explicit tenant/job/page-slot identities and schema `2.0`;
the aggregator checks scoped keys, bucket, identity and source before publishing
canonical document results. OCR coordinates use preprocessed pixels; preserved
native PDF text uses PDF-point provenance and separate artifact references.
`PREPROCESS_MAX_TOTAL_PIXELS` bounds total decoded pixels, not total process RSS.
The contracts are still versioned dictionaries, not exhaustive typed token/zone/
field schemas; comprehensive field evidence and calibrated probabilities remain
acceptance work. See service READMEs and `infra/quality/README.md`.

## Resolution And Delivery Helpers (2026-10-01)

`operator_auth.py` derives tenant/operator identity from separate
`IDP_OPERATOR_API_KEYS` mappings through `X-IDP-Operator-Key`. Matching uses
constant-time comparison; actor IDs are credential fingerprints. Missing/invalid
keys return 401, tenant-header impersonation returns 403. Intake keys never grant
operator access. Private delivery operations additionally require the internal
service key. A shared service key is a trusted-service identity, not end-user
authorization; keep these services private.

`storage.download_verified_bytes` bounds source reads by the upload limit,
verifies declared length and SHA-256, and closes streams on all outcomes. It is
used before a replacement job is registered and again by copy recovery. Storage
outages are not checksum failures and do not become synthetic document text.

Configuration adds operator mappings, durable delivery attempt/lease/reconcile
controls, and Temporal delivery wait/poll values. See the ingestion and delivery
READMEs plus `.env.example` for defaults. Separate keys, database backups and
protected transport are still required for deployment.

`tenant_scope.py` defines artifact keys and rejects references outside the request's tenant/job. The default tenant retains `jobs/<job_id>/...`; other tenants use `tenants/<tenant_id>/jobs/<job_id>/...`. Tenant and job IDs are validated before a key is built. Services must call `require_artifact_scope` before reading a request-supplied key and `artifact_key` for every new artifact.

`internal_auth.py` provides service-to-service authentication. Set `IDP_REQUIRE_INTERNAL_AUTH=true` and a strong `IDP_INTERNAL_API_KEY` in staging and production. The orchestrator sends it as `X-IDP-Internal-Key`. Health, readiness, metrics, and OpenAPI paths are exempt from the middleware. The ingestion gateway uses its own tenant-scoped API keys; review decision APIs use separate reviewer and polling credentials.

`storage.py` now also exposes `object_exists`, which distinguishes an absent S3 object from a storage outage when ingestion reconciles interrupted uploads. Retention deletes tenant-prefixed artifacts, expired outbox records, and expired review decisions.

`page_contract.py` defines schema `2.0` and validates ordered one-based page manifests, first-page aliases, and tenant/job-scoped page keys. OCR and layout use it to reject cross-page or cross-tenant artifact references.

## 2026-10-01: Storage Integrity and Boundary Gate

`storage.verify_artifact` streams a stored raw object, verifies registered byte
length and SHA-256, and closes the S3 response stream even on mismatch. Ingestion
uses it before accepting an interrupted upload for dispatch. A missing object,
corrupt object, and transient storage outage are separate outcomes.

`require_artifact_scope` also rejects dot segments, repeated path separators,
backslashes, and percent-encoded paths. Its prefix check is a request-boundary
guard, not a replacement for private service networks, encrypted transport, or
tenant-scoped public authorization. All eight stage HTTP boundaries are exercised
by `scripts/verify_tenant_boundaries.py`; run the complete gate with
`bash scripts/run_ingestion_verification.sh`. Fixtures prove denial/recovery, not
production-quality OCR or extraction accuracy.
