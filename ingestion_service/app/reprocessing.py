"""Operator resolution creates a durable, linked processing generation."""
import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import Settings, get_settings
from shared.idp_common.operator_auth import Operator, resolve_operator
from shared.idp_common.storage import download_verified_bytes, upload_bytes
from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope


class ReprocessRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


def request_fingerprint(reason: str, operator: Operator) -> str:
    return hashlib.sha256(json.dumps({"reason": reason, "actor": operator.subject}, sort_keys=True).encode()).hexdigest()


async def fetch_exceptions(settings: Settings, tenant: str, suffix: str = "", **params) -> Any:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(settings.review_url + "/review/ocr-exceptions" + suffix,
                                        params={"tenant_id": tenant, **params},
                                        headers={"X-IDP-Review-Key": settings.review_decision_api_key})
        if response.status_code == 404:
            raise HTTPException(404, "OCR exception not found")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Exception repository unavailable") from exc


async def persist_reprocess_artifact(job: dict[str, Any], settings: Settings) -> None:
    source = job["metadata"]["reprocess_source"]
    if source["tenant_id"] != job["tenant_id"] or source["bucket"] != settings.raw_bucket:
        raise ValueError("Reprocessing source is outside tenant/raw scope")
    require_artifact_scope(job["tenant_id"], source["job_id"], source["key"])
    data = await asyncio.to_thread(download_verified_bytes, settings, source["bucket"], source["key"], job["sha256"], job["size_bytes"])
    key = artifact_key(job["tenant_id"], job["job_id"], "raw/" + job["original_filename"])
    await asyncio.to_thread(upload_bytes, settings, settings.raw_bucket, key, data, job["content_type"])


async def reconcile_reprocess_results(repository, client) -> None:
    for row in await asyncio.to_thread(repository.list_reprocess_inflight):
        try:
            await reconcile_reprocess_result(repository, client, row)
        except Exception:
            logging.getLogger('ingestion_service').exception('Resolution reconciliation deferred child_job_id=%s', row['child_job_id'])
        finally:
            # Rotate the bounded batch even when a long-running or poisoned record cannot finish.
            await asyncio.to_thread(repository.record_reprocess_check, row['tenant_id'], row['parent_job_id'])


async def reconcile_reprocess_result(repository, client, row) -> None:
    if row["job_status"] == "FAILED":
        await asyncio.to_thread(repository.set_reprocess_status, row["tenant_id"], row["parent_job_id"], "needs_attention", "raw_artifact_failed")
    elif row["workflow_id"]:
        handle = client.get_workflow_handle(row["workflow_id"])
        description = await handle.describe()
        if description.status.name == "RUNNING":
            return
        if description.status.name == "COMPLETED":
            result = await handle.result()
            outcome = str(result.get("status") or "unknown")
        else:
            outcome = description.status.name.lower()
        await asyncio.to_thread(repository.set_reprocess_status, row["tenant_id"], row["parent_job_id"],
                                "resolved" if outcome == "delivered" else "needs_attention", outcome)


def install_reprocessing(app, get_repository, get_client, dispatch):
    @app.get("/exceptions")
    async def list_exceptions(limit: int = 50, offset: int = 0, operator: Operator = Depends(resolve_operator),
                              settings: Settings = Depends(get_settings), repository=Depends(get_repository)):
        data = await fetch_exceptions(settings, operator.tenant_id, limit=max(1, min(limit, 200)), offset=max(0, offset))
        for record in data["exceptions"]:
            record["resolution"] = await asyncio.to_thread(repository.get_reprocess_request, operator.tenant_id, record["job_id"])
        return data

    @app.get("/exceptions/{job_id}")
    async def get_exception(job_id: str, operator: Operator = Depends(resolve_operator),
                            settings: Settings = Depends(get_settings), repository=Depends(get_repository)):
        record = await fetch_exceptions(settings, operator.tenant_id, "/" + job_id)
        record["resolution"] = await asyncio.to_thread(repository.get_reprocess_request, operator.tenant_id, job_id)
        return record

    @app.post("/exceptions/{job_id}/reprocess", status_code=202)
    async def reprocess(job_id: str, request: ReprocessRequest, idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=256),
                        operator: Operator = Depends(resolve_operator), settings: Settings = Depends(get_settings), repository=Depends(get_repository)):
        existing = await asyncio.to_thread(repository.get_reprocess_request, operator.tenant_id, job_id)
        fingerprint = request_fingerprint(request.reason, operator)
        if existing:
            if existing["request_key"] != idempotency_key or existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "An immutable reprocessing request already exists for this exception")
            return {**existing, "idempotent_replay": True}
        parent = await asyncio.to_thread(repository.get_job_by_id, operator.tenant_id, job_id)
        if not parent:
            raise HTTPException(404, "Job not found")
        await fetch_exceptions(settings, operator.tenant_id, "/" + job_id)
        try:
            client = await get_client(settings)
            handle = client.get_workflow_handle(parent["workflow_id"] or f"idp-{operator.tenant_id}-{job_id}")
            terminal_exception = (await handle.describe()).status.name == "COMPLETED" and (await handle.result()).get("status") == "ocr_exception"
        except Exception as exc:
            raise HTTPException(503, "Workflow state unavailable; exception remains unresolved") from exc
        if not terminal_exception:
            raise HTTPException(409, "Only terminal OCR exceptions can be reprocessed")
        if parent["artifact_bucket"] != settings.raw_bucket or not parent["artifact_key"]:
            raise HTTPException(409, "Verified source artifact is unavailable")
        require_artifact_scope(operator.tenant_id, job_id, parent["artifact_key"])
        try:
            data = await asyncio.to_thread(download_verified_bytes, settings, parent["artifact_bucket"], parent["artifact_key"], parent["sha256"], parent["size_bytes"])
        except ValueError as exc:
            raise HTTPException(409, "Source artifact failed integrity validation") from exc
        except Exception as exc:
            raise HTTPException(503, "Source artifact unavailable; exception remains unresolved") from exc
        try:
            resolution = await asyncio.to_thread(repository.create_reprocess_job, operator.tenant_id, job_id, str(uuid.uuid4()),
                                                  idempotency_key, fingerprint, operator.subject, request.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        child_id = resolution["child_job_id"]
        child = await asyncio.to_thread(repository.get_job_by_id, operator.tenant_id, child_id)
        key = artifact_key(operator.tenant_id, child_id, "raw/" + child["original_filename"])
        try:
            await asyncio.to_thread(upload_bytes, settings, settings.raw_bucket, key, data, child["content_type"])
            await asyncio.to_thread(repository.update_artifact, child_id, settings.raw_bucket, key, f"s3://{settings.raw_bucket}/{key}")
            claimed = await asyncio.to_thread(repository.claim_dispatch, child_id)
            if claimed:
                await dispatch(repository, settings, claimed)
        except Exception:
            # Persisted source metadata lets the normal reconciler finish after a crash/outage.
            logging.getLogger('ingestion_service').warning('Reprocessing artifact/dispatch deferred child_job_id=%s', child_id)
        resolution = await asyncio.to_thread(repository.get_reprocess_request, operator.tenant_id, job_id)
        return {**resolution, "idempotent_replay": False}
