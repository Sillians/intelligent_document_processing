"""Exercise real intake idempotency and tenant denial at HTTP stage boundaries."""
import asyncio
import json
from pathlib import Path
import uuid

import httpx
from temporalio.client import Client

from shared.idp_common.config import get_settings


async def verify():
    settings = get_settings()
    checks = []
    output = Path("artifacts/ingestion-recovery/tenant-boundaries.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('{"passed": false}')
    headers = {"X-API-Key": "local-verification-intake", "X-Tenant-Id": "verification"}
    base = "http://ingestion-service:8000"
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(30):
            try:
                response = await client.get(base + "/health")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Intake did not become ready")
        sample = Path("samples/documents/sample_invoice_001.png").read_bytes() + uuid.uuid4().hex.encode()
        key = uuid.uuid4().hex

        async def submit(data, tenant_headers, idem):
            return await client.post(base + "/documents", files={"file": ("invoice.png", data, "image/png")}, headers={**tenant_headers, "Idempotency-Key": idem})

        first = await submit(sample, headers, key)
        first.raise_for_status()
        job = first.json()["job_id"]
        alias = uuid.uuid4().hex
        duplicate = await submit(sample, headers, alias)
        duplicate.raise_for_status()
        assert duplicate.json()["job_id"] == job and duplicate.json()["deduplicated"]
        replay = await submit(sample, headers, alias)
        assert replay.status_code == 200 and replay.json()["idempotency_replay"]
        assert (await submit(sample + b"different", headers, alias)).status_code == 409
        checks.append("dedupe alias is durable; changed content returns 409")

        concurrent_sample = sample + uuid.uuid4().hex.encode()
        concurrent = await asyncio.gather(*[submit(concurrent_sample, headers, uuid.uuid4().hex) for _ in range(4)])
        assert all(response.status_code == 200 for response in concurrent), [response.text for response in concurrent]
        concurrent_jobs = {response.json()["job_id"] for response in concurrent}
        assert len(concurrent_jobs) == 1, concurrent_jobs
        checks.append("four concurrent equal-content requests with different keys create one job")

        other = {"X-API-Key": "local-verification-intake-other", "X-Tenant-Id": "other"}
        for suffix in ("", "/audit", "/result"):
            assert (await client.get(base + f"/documents/{job}{suffix}", headers=other)).status_code == 404
        assert (await client.get(base + f"/documents/{job}", headers={**headers, "X-Tenant-Id": "other"})).status_code == 403
        assert (await client.get(base + f"/documents/{job}", headers={"X-API-Key": "invalid"})).status_code == 401
        checks.append("intake status, audit, result and tenant-header impersonation denied")
        other_submission = await submit(sample, other, key)
        other_submission.raise_for_status()
        other_job = other_submission.json()["job_id"]
        assert other_job != job
        assert f"tenants/other/jobs/{other_job}/" in other_submission.json()["artifact_uri"]
        checks.append("same key and content in another tenant create a separately scoped job")

        wrong = f"tenants/other/jobs/{job}/artifact.json"
        common = {"tenant_id": "verification", "job_id": job}
        probes = [
            (settings.preprocess_url, "/preprocess", {"raw_bucket": settings.raw_bucket, "raw_key": wrong}),
            (settings.ocr_url, "/ocr", {"preprocessed_key": wrong}),
            (settings.layout_url, "/layout", {"preprocessed_key": wrong, "ocr_key": wrong}),
            (settings.classifier_url, "/route", {"ocr_key": wrong, "layout_key": wrong}),
            (settings.extraction_url, "/extract", {"ocr_key": wrong, "layout_key": wrong, "route": "invoice", "ocr_confidence": 0.9}),
            (settings.validation_url, "/validate", {"extraction_key": wrong, "fields": {}, "confidence": 0.9, "used_vlm_fallback": False}),
            (settings.review_url, "/review/tasks", {"extraction_key": wrong, "fields": {}, "confidence": 0.9}),
            (settings.delivery_url, "/deliver", {"extraction_key": wrong, "payload": {}, "approval_status": "auto_approved"}),
        ]
        internal = {"X-IDP-Internal-Key": settings.idp_internal_api_key}
        for url, path, extra in probes:
            missing = await client.post(url + path, json={**common, **extra})
            assert missing.status_code == 401, (path, "unauthenticated", missing.status_code, missing.text)
            denied = await client.post(url + path, json={**common, **extra}, headers=internal)
            assert denied.status_code == 403, (path, denied.status_code, denied.text)
            checks.append(path + " requires internal authorization and denies cross-tenant artifacts")
        denied = await client.get(settings.review_url + f"/review/ocr-exceptions/{job}", params={"tenant_id": "verification"}, headers={"X-IDP-Review-Key": "local-verification-review-other"})
        assert denied.status_code == 401
        checks.append("review operator key cannot impersonate another tenant")
    temporal = await Client.connect(settings.temporal_address)
    await temporal.get_workflow_handle(f"idp-verification-{job}").cancel()
    for concurrent_job in concurrent_jobs:
        await temporal.get_workflow_handle(f"idp-verification-{concurrent_job}").cancel()
    await temporal.get_workflow_handle(f"idp-other-{other_job}").cancel()
    report = {"passed": True, "checks": checks}
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(verify())
