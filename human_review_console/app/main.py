from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import time
from typing import Any, Protocol
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_json, upload_json

settings = get_settings()
logger = logging.getLogger("human_review_console")
_REVIEW_INFLIGHT = 0
_REVIEW_INFLIGHT_LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="human-review-console", lifespan=lifespan)
instrument_app(app, "human-review-console")


class ReviewTaskRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    reasons: list[str] = Field(default_factory=list)
    fields: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    route: str | None = Field(default=None, max_length=64)
    verdict: str | None = Field(default=None, max_length=64)
    priority: str | None = Field(default=None, max_length=32)
    assignee: str | None = Field(default=None, max_length=128)
    extraction_bucket: str | None = Field(default=None, max_length=128)
    extraction_key: str | None = Field(default=None, max_length=1024)
    validation_bucket: str | None = Field(default=None, max_length=128)
    validation_key: str | None = Field(default=None, max_length=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    status: str
    provider: str
    external_task_id: str
    external_url: str = ""
    raw_response: Any = None


class ReviewProvider(Protocol):
    name: str

    async def create_task(self, task: ReviewTaskRequest, *, review_task_id: str, payload: dict[str, Any]) -> ProviderResult:
        ...


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def build_review_task_id(task: ReviewTaskRequest) -> str:
    # Make task creation idempotent for repeated workflow retries of the same review request.
    digest = hashlib.sha256(
        _stable_json(
            {
                "job_id": task.job_id,
                "reasons": sorted(task.reasons),
                "fields": task.fields,
                "confidence": round(task.confidence, 4),
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"review-{digest}"


def derive_priority(task: ReviewTaskRequest) -> str:
    if task.priority:
        return task.priority.strip().lower()
    if task.confidence < float(getattr(settings, "review_high_priority_confidence_threshold", 0.50)):
        return "high"
    if any(reason.startswith("reject_") or reason == "reject_unusable_extraction" for reason in task.reasons):
        return "high"
    if task.confidence < settings.auto_approve_threshold:
        return "medium"
    return "normal"


def build_review_payload(task: ReviewTaskRequest, *, review_task_id: str) -> dict[str, Any]:
    return {
        "review_task_id": review_task_id,
        "job_id": task.job_id,
        "route": task.route or "unknown",
        "verdict": task.verdict or "needs_review",
        "priority": derive_priority(task),
        "reasons": task.reasons,
        "fields": task.fields,
        "confidence": round(task.confidence, 4),
        "assignee": task.assignee or "",
        "source": {
            "extraction_bucket": task.extraction_bucket or "",
            "extraction_key": task.extraction_key or "",
            "validation_bucket": task.validation_bucket or "",
            "validation_key": task.validation_key or "",
        },
        "metadata": task.metadata,
    }


class LocalQueueProvider:
    name = "local_queue"

    async def create_task(self, task: ReviewTaskRequest, *, review_task_id: str, payload: dict[str, Any]) -> ProviderResult:
        return ProviderResult(
            status="queued_without_label_studio",
            provider=self.name,
            external_task_id=review_task_id,
        )


class LabelStudioProvider:
    name = "label_studio"

    def _import_url(self) -> str:
        base_url = settings.label_studio_url.rstrip("/")
        return f"{base_url}/api/projects/{settings.label_studio_project_id}/import"

    def _project_url(self) -> str:
        base_url = settings.label_studio_url.rstrip("/")
        return f"{base_url}/projects/{settings.label_studio_project_id}"

    async def create_task(self, task: ReviewTaskRequest, *, review_task_id: str, payload: dict[str, Any]) -> ProviderResult:
        if not settings.label_studio_token:
            raise RuntimeError("LABEL_STUDIO_TOKEN is not configured")

        task_payload = [
            {
                "data": {
                    "review_task_id": review_task_id,
                    "job_id": task.job_id,
                    "route": payload["route"],
                    "verdict": payload["verdict"],
                    "priority": payload["priority"],
                    "reasons": task.reasons,
                    "extracted_fields": task.fields,
                    "confidence": round(task.confidence, 4),
                    "source": payload["source"],
                    "metadata": task.metadata,
                }
            }
        ]
        headers = {"Authorization": f"Token {settings.label_studio_token}"}
        timeout = max(1, int(getattr(settings, "review_label_studio_timeout_seconds", 30)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self._import_url(), headers=headers, json=task_payload)
            response.raise_for_status()
            data = response.json()

        external_task_id = extract_label_studio_task_ref(data, default=review_task_id)
        return ProviderResult(
            status="queued_in_label_studio",
            provider=self.name,
            external_task_id=external_task_id,
            external_url=self._project_url(),
            raw_response=data,
        )


def extract_label_studio_task_ref(data: Any, *, default: str) -> str:
    if isinstance(data, dict):
        for key in ("task_id", "id"):
            value = data.get(key)
            if value is not None:
                return str(value)
        task_ids = data.get("task_ids")
        if isinstance(task_ids, list) and task_ids:
            return str(task_ids[0])
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            for key in ("id", "task_id"):
                value = first.get(key)
                if value is not None:
                    return str(value)
    return default


def select_provider() -> ReviewProvider:
    provider = str(getattr(settings, "review_provider", "auto")).strip().lower()
    if provider == "label_studio":
        return LabelStudioProvider()
    if provider == "local_queue":
        return LocalQueueProvider()
    if settings.label_studio_token:
        return LabelStudioProvider()
    return LocalQueueProvider()


def _persist_review_record(task: ReviewTaskRequest, record: dict[str, Any]) -> tuple[str, str]:
    key = f"jobs/{task.job_id}/review/{record['review_task_id']}.json"
    review_bucket = getattr(settings, "review_bucket", "review-artifacts")
    try:
        upload_json(settings, review_bucket, key, record)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "primary review artifact upload failed job_id=%s bucket=%s",
            task.job_id,
            review_bucket,
            exc_info=exc,
        )
        fallback_bucket = settings.validation_bucket
        if fallback_bucket == review_bucket:
            raise
        upload_json(settings, fallback_bucket, key, record)
        review_bucket = fallback_bucket
        logger.warning("review artifact upload fell back job_id=%s fallback_bucket=%s", task.job_id, fallback_bucket)
    return review_bucket, key


def _load_review_record(job_id: str, review_task_id: str) -> dict[str, Any]:
    key = f"jobs/{job_id}/review/{review_task_id}.json"
    bucket = getattr(settings, "review_bucket", "review-artifacts")
    try:
        return download_json(settings, bucket, key)
    except Exception as exc:  # noqa: BLE001
        fallback_bucket = settings.validation_bucket
        if fallback_bucket == bucket:
            raise HTTPException(status_code=404, detail="review task not found") from exc
        try:
            return download_json(settings, fallback_bucket, key)
        except Exception as fallback_exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="review task not found") from fallback_exc


async def _try_acquire_request_slot() -> bool:
    global _REVIEW_INFLIGHT
    async with _REVIEW_INFLIGHT_LOCK:
        max_inflight = max(1, int(getattr(settings, "review_max_inflight_requests", 16)))
        if _REVIEW_INFLIGHT >= max_inflight:
            return False
        _REVIEW_INFLIGHT += 1
        return True


async def _release_request_slot() -> None:
    global _REVIEW_INFLIGHT
    async with _REVIEW_INFLIGHT_LOCK:
        _REVIEW_INFLIGHT = max(0, _REVIEW_INFLIGHT - 1)


@app.get("/health")
async def health() -> dict[str, str]:
    provider = select_provider().name
    return {"status": "ok", "provider": provider}


@app.post("/review/tasks")
async def create_review_task(task: ReviewTaskRequest) -> dict[str, Any]:
    if not await _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="review worker is busy")

    started = time.perf_counter()
    try:
        review_task_id = build_review_task_id(task)
        payload = build_review_payload(task, review_task_id=review_task_id)
        provider = select_provider()
        timeout = max(1, int(getattr(settings, "review_request_timeout_seconds", 30)))
        try:
            provider_result = await asyncio.wait_for(
                provider.create_task(task, review_task_id=review_task_id, payload=payload),
                timeout=timeout,
            )
        except (httpx.HTTPError, RuntimeError, asyncio.TimeoutError) as exc:
            if not bool(getattr(settings, "review_fallback_to_local_queue", True)):
                raise HTTPException(status_code=502, detail=f"review provider failed: {exc}") from exc
            logger.warning("review provider failed; falling back to local queue job_id=%s error=%s", task.job_id, exc)
            provider_result = await LocalQueueProvider().create_task(task, review_task_id=review_task_id, payload=payload)
            payload["provider_failure"] = str(exc)[:500]

        record = {
            **payload,
            "review_status": provider_result.status,
            "provider": provider_result.provider,
            "external_task_id": provider_result.external_task_id,
            "external_url": provider_result.external_url,
            "provider_response": provider_result.raw_response,
            "created_at_unix": round(time.time(), 3),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        if bool(getattr(settings, "review_persist_tasks", True)):
            bucket, key = await asyncio.to_thread(_persist_review_record, task, record)
            record["review_bucket"] = bucket
            record["review_task_key"] = key

        return {
            "job_id": task.job_id,
            "review_status": provider_result.status,
            "review_ref": provider_result.external_task_id,
            "review_task_id": review_task_id,
            "review_bucket": record.get("review_bucket"),
            "review_task_key": record.get("review_task_key"),
            "provider": provider_result.provider,
            "priority": payload["priority"],
        }
    finally:
        await _release_request_slot()


@app.get("/review/tasks/{job_id}/{review_task_id}")
async def get_review_task(job_id: str, review_task_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_load_review_record, job_id, review_task_id)
