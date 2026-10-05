from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Protocol
import uuid

import httpx
from botocore.exceptions import ClientError
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_json, upload_json
from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope
from shared.idp_common.internal_auth import internal_key_valid
from human_review_console.app.decision_repository import ReviewDecisionRepository

settings = get_settings()
logger = logging.getLogger("human_review_console")
_REVIEW_INFLIGHT = 0
_REVIEW_INFLIGHT_LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.decision_repository = await asyncio.to_thread(ReviewDecisionRepository, settings)
    try:
        yield
    finally:
        await asyncio.to_thread(app.state.decision_repository.close)


app = FastAPI(title="human-review-console", lifespan=lifespan)
instrument_app(app, "human-review-console")


class ReviewTaskRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
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


class ReviewDecisionRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    reviewer_id: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern="^(approved|rejected)$")
    fields: dict[str, Any] = Field(default_factory=dict)


class OCRExceptionRequest(BaseModel):
    page_number: int | None = Field(default=None, ge=1, le=1000)
    tenant_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    failure_code: str = Field(pattern="^OCR_[A-Z_]+$")


@app.post("/review/ocr-exceptions")
async def record_ocr_exception(request: OCRExceptionRequest, x_idp_review_key: str | None = Header(default=None)):
    _require_decision_key(x_idp_review_key)
    require_artifact_scope(request.tenant_id, request.job_id, artifact_key(request.tenant_id, request.job_id, "ocr/exception.json"))
    record = {"tenant_id": request.tenant_id, "job_id": request.job_id,
              "review_task_id": "ocr-exception", "decision": "exception",
              "fields": {}, "failure_code": request.failure_code,
              "page_number": request.page_number,
              "status": "ocr_exception", "created_at_unix": time.time()}
    return await asyncio.to_thread(app.state.decision_repository.create, record)


@app.get("/review/ocr-exceptions/{job_id}")
async def get_ocr_exception(job_id: str, tenant_id: str, x_idp_review_key: str | None = Header(default=None)):
    _require_exception_read_key(x_idp_review_key, tenant_id)
    record = await asyncio.to_thread(_load_decision, job_id, "ocr-exception", tenant_id)
    if not record:
        raise HTTPException(status_code=404, detail="OCR exception not found")
    return record


def _require_exception_read_key(value: str | None, tenant_id: str) -> None:
    if settings.review_decision_api_key and value and hmac.compare_digest(value, settings.review_decision_api_key):
        return
    _require_decision_key(value, tenant_id)


@app.get("/review/ocr-exceptions")
async def list_ocr_exceptions(tenant_id: str, limit: int = 50, offset: int = 0,
                              x_idp_review_key: str | None = Header(default=None)):
    _require_exception_read_key(x_idp_review_key, tenant_id)
    return {"exceptions": await asyncio.to_thread(app.state.decision_repository.list_exceptions,
                                                  tenant_id, max(1, min(limit, 200)), max(0, offset))}


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
                "tenant_id": task.tenant_id,
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
        "tenant_id": task.tenant_id,
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

    def _base_url(self) -> str:
        return settings.label_studio_url.rstrip("/")

    def _import_url(self) -> str:
        return f"{self._base_url()}/api/projects/{settings.label_studio_project_id}/import"

    def _project_url(self) -> str:
        return f"{self._base_url()}/projects/{settings.label_studio_project_id}"

    def _project_api_url(self) -> str:
        return f"{self._base_url()}/api/projects/{settings.label_studio_project_id}/"

    async def _authorization_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        token = settings.label_studio_token.strip()
        if not token:
            raise RuntimeError("LABEL_STUDIO_TOKEN is not configured")

        auth_mode = label_studio_auth_mode(token)
        if auth_mode == "token":
            return {"Authorization": f"Token {token}"}
        if auth_mode == "bearer":
            return {"Authorization": f"Bearer {token}"}

        response = await client.post(
            f"{self._base_url()}/api/token/refresh",
            json={"refresh": token},
        )
        response.raise_for_status()
        access_token = str(response.json().get("access") or "").strip()
        if not access_token:
            raise RuntimeError("Label Studio token refresh did not return an access token")
        return {"Authorization": f"Bearer {access_token}"}

    async def check_connection(self) -> dict[str, Any]:
        timeout = max(1, int(getattr(settings, "review_label_studio_timeout_seconds", 30)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = await self._authorization_headers(client)
            response = await client.get(self._project_api_url(), headers=headers)
            response.raise_for_status()
            project = response.json()
        return {
            "status": "ok",
            "provider": self.name,
            "project_id": settings.label_studio_project_id,
            "project_title": str(project.get("title") or ""),
            "auth_mode": label_studio_auth_mode(settings.label_studio_token),
        }

    async def create_task(self, task: ReviewTaskRequest, *, review_task_id: str, payload: dict[str, Any]) -> ProviderResult:
        if not settings.label_studio_token:
            raise RuntimeError("LABEL_STUDIO_TOKEN is not configured")

        task_payload = [
            {
                "data": {
                    "review_task_id": review_task_id,
                    "job_id": task.job_id,
                    "tenant_id": task.tenant_id,
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
        timeout = max(1, int(getattr(settings, "review_label_studio_timeout_seconds", 30)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = await self._authorization_headers(client)
            response = await client.post(self._import_url(), params={"return_task_ids": "true"}, headers=headers, json=task_payload)
            response.raise_for_status()
            data = response.json()

        external_task_id = extract_label_studio_task_ref(data, default="")
        if not external_task_id:
            raise RuntimeError("Label Studio import did not return a task ID")
        return ProviderResult(
            status="queued_in_label_studio",
            provider=self.name,
            external_task_id=external_task_id,
            external_url=self._project_url(),
            raw_response=data,
        )


def label_studio_auth_mode(token: str) -> str:
    configured = str(getattr(settings, "label_studio_auth_scheme", "token") or "token").strip().lower()
    if configured == "auto":
        return "pat" if token.count(".") == 2 else "token"
    if configured not in {"token", "pat", "bearer"}:
        raise RuntimeError(f"Unsupported LABEL_STUDIO_AUTH_SCHEME: {configured}")
    return configured


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
    key = artifact_key(task.tenant_id, task.job_id, f"review/{record['review_task_id']}.json")
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


def _load_review_record(job_id: str, review_task_id: str, tenant_id: str = "default") -> dict[str, Any]:
    key = artifact_key(tenant_id, job_id, f"review/{review_task_id}.json")
    bucket = getattr(settings, "review_bucket", "review-artifacts")
    transient_error: Exception | None = None
    try:
        return download_json(settings, bucket, key)
    except Exception as exc:  # noqa: BLE001
        if not isinstance(exc, ClientError) or str(exc.response.get("Error", {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
            transient_error = exc
        fallback_bucket = settings.validation_bucket
        if fallback_bucket == bucket:
            raise HTTPException(status_code=503 if transient_error else 404, detail="review record unavailable" if transient_error else "review task not found") from exc
        try:
            return download_json(settings, fallback_bucket, key)
        except Exception as fallback_exc:  # noqa: BLE001
            if not isinstance(fallback_exc, ClientError) or str(fallback_exc.response.get("Error", {}).get("Code", "")) not in {"404", "NoSuchKey", "NotFound"}:
                transient_error = fallback_exc
            raise HTTPException(status_code=503 if transient_error else 404, detail="review record unavailable" if transient_error else "review task not found") from fallback_exc


def _decision_key(job_id: str, review_task_id: str, tenant_id: str = "default") -> str:
    return artifact_key(tenant_id, job_id, f"review/{review_task_id}.decision.json")


def _load_decision(job_id: str, review_task_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
    return app.state.decision_repository.get(tenant_id, job_id, review_task_id)


def _save_decision(job_id: str, review_task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    stored = app.state.decision_repository.create(decision)
    if stored["decision"] != decision["decision"] or stored["fields"] != decision["fields"]:
        raise HTTPException(status_code=409, detail="Review decision already recorded")
    try:
        upload_json(settings, settings.review_bucket, _decision_key(job_id, review_task_id, decision["tenant_id"]), stored)
    except Exception:  # noqa: BLE001
        logger.exception("Review decision audit artifact upload failed job_id=%s", job_id)
    return stored


def _require_decision_key(value: str | None, tenant_id: str | None = None) -> None:
    if tenant_id is None:
        expected = settings.review_decision_api_key
    else:
        entries = (item.strip().split(":", 1) for item in settings.review_decision_api_keys.split(",") if ":" in item)
        expected = next((key for key, tenant in entries if tenant == tenant_id), "")
    if not expected or not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Invalid review decision credential")


def _checked_review(job_id: str, review_task_id: str, tenant_id: str) -> dict[str, Any]:
    record = _load_review_record(job_id, review_task_id, tenant_id)
    if record.get("job_id") != job_id or record.get("review_task_id") != review_task_id or record.get("tenant_id", "default") != tenant_id:
        raise HTTPException(status_code=404, detail="review task not found")
    return record


def _fields_from_label_studio(task: dict[str, Any]) -> tuple[str, dict[str, Any], str] | None:
    annotations = task.get("annotations") or []
    completed = [item for item in annotations if isinstance(item, dict) and not item.get("was_cancelled") and item.get("result")]
    if not completed:
        return None
    annotation = completed[-1]
    fields: dict[str, Any] = {}
    decision = None
    for item in annotation["result"]:
        name = item.get("from_name")
        value = item.get("value") or {}
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        if name == "decision":
            choices = value.get("choices") or []
            if len(choices) == 1:
                choice = str(choices[0]).lower()
                if choice in {"approve", "approved"}:
                    decision = "approved"
                elif choice in {"reject", "rejected"}:
                    decision = "rejected"
            continue
        texts = value.get("text") or []
        if len(texts) == 1:
            if name == "corrected_fields_json":
                try:
                    parsed = json.loads(texts[0])
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    fields.update(parsed)
            else:
                fields[name] = texts[0]
    if decision is None:
        return None
    reviewer = annotation.get("completed_by")
    if isinstance(reviewer, dict):
        reviewer = reviewer.get("id")
    if isinstance(reviewer, bool) or not isinstance(reviewer, (str, int)) or not str(reviewer).strip():
        return None
    reviewer = str(reviewer)
    return decision, fields, reviewer


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
async def health() -> dict[str, Any]:
    provider = select_provider().name
    return {
        "status": "ok",
        "provider": provider,
        "label_studio_configured": bool(settings.label_studio_token),
    }


@app.get("/health/provider")
async def provider_health() -> dict[str, Any]:
    provider = select_provider()
    if not isinstance(provider, LabelStudioProvider):
        return {
            "status": "disabled",
            "provider": provider.name,
            "label_studio_configured": False,
        }
    try:
        return await provider.check_connection()
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("Label Studio provider health check failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Label Studio provider unavailable: {exc}") from exc


@app.post("/review/tasks")
async def create_review_task(task: ReviewTaskRequest, x_idp_internal_key: str | None = Header(default=None)) -> dict[str, Any]:
    if not internal_key_valid(x_idp_internal_key):
        raise HTTPException(status_code=401, detail="Invalid internal service credential")
    for key in (task.extraction_key, task.validation_key):
        if key:
            require_artifact_scope(task.tenant_id, task.job_id, key)
    if not await _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="review worker is busy")

    started = time.perf_counter()
    try:
        review_task_id = build_review_task_id(task)
        if settings.review_persist_tasks:
            try:
                existing = await asyncio.to_thread(_checked_review, task.job_id, review_task_id, task.tenant_id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            else:
                return {"job_id": task.job_id, "tenant_id": task.tenant_id,
                        "review_status": existing["review_status"], "review_ref": existing["external_task_id"],
                        "review_task_id": review_task_id, "provider": existing["provider"],
                        "review_bucket": settings.review_bucket,
                        "review_task_key": artifact_key(task.tenant_id, task.job_id, f"review/{review_task_id}.json"),
                        "priority": existing["priority"]}
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
            "tenant_id": task.tenant_id,
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
async def get_review_task(job_id: str, review_task_id: str, tenant_id: str = "default", x_idp_internal_key: str | None = Header(default=None)) -> dict[str, Any]:
    if not internal_key_valid(x_idp_internal_key):
        raise HTTPException(status_code=401, detail="Invalid internal service credential")
    return await asyncio.to_thread(_load_review_record, job_id, review_task_id, tenant_id)


@app.post("/review/tasks/{job_id}/{review_task_id}/decision")
async def submit_review_decision(
    job_id: str,
    review_task_id: str,
    request: ReviewDecisionRequest,
    x_idp_review_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_decision_key(x_idp_review_key, request.tenant_id)
    record = await asyncio.to_thread(_checked_review, job_id, review_task_id, request.tenant_id)
    existing = await asyncio.to_thread(_load_decision, job_id, review_task_id, request.tenant_id)
    if existing:
        if existing["decision"] != request.decision or existing["fields"] != request.fields:
            raise HTTPException(status_code=409, detail="Review decision already recorded")
        return existing
    if request.decision == "approved" and not request.fields:
        raise HTTPException(status_code=422, detail="Approved review requires corrected fields")
    decision = {
        "job_id": job_id,
        "tenant_id": request.tenant_id,
        "review_task_id": review_task_id,
        "decision": request.decision,
        "fields": request.fields,
        "reviewer_id": request.reviewer_id,
        "source": record["provider"],
        "decided_at_unix": round(time.time(), 3),
        "decision_key": _decision_key(job_id, review_task_id, request.tenant_id),
    }
    stored = await asyncio.to_thread(_save_decision, job_id, review_task_id, decision)
    return stored or decision


@app.get("/review/tasks/{job_id}/{review_task_id}/decision")
async def poll_review_decision(
    job_id: str,
    review_task_id: str,
    tenant_id: str,
    x_idp_review_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_decision_key(x_idp_review_key)
    record = await asyncio.to_thread(_checked_review, job_id, review_task_id, tenant_id)
    existing = await asyncio.to_thread(_load_decision, job_id, review_task_id, tenant_id)
    if existing:
        return {"status": "decided", **existing}
    if record["provider"] != "label_studio":
        return {"status": "pending"}
    provider = LabelStudioProvider()
    timeout = settings.review_label_studio_timeout_seconds
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = await provider._authorization_headers(client)
            response = await client.get(f"{provider._base_url()}/api/tasks/{record['external_task_id']}/", headers=headers)
            response.raise_for_status()
            task = response.json()
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Label Studio review lookup failed") from exc
    if str(task.get("project")) != str(settings.label_studio_project_id):
        raise HTTPException(status_code=502, detail="Label Studio task belongs to a different project")
    data = task.get("data") or {}
    if data.get("job_id") != job_id or data.get("review_task_id") != review_task_id or data.get("tenant_id", "default") != tenant_id:
        raise HTTPException(status_code=502, detail="Label Studio task identity mismatch")
    parsed = _fields_from_label_studio(task)
    if not parsed:
        return {"status": "pending"}
    outcome, corrected, reviewer = parsed
    if outcome == "approved" and not corrected:
        return {"status": "pending", "reason": "annotation_has_no_fields"}
    decision = {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "review_task_id": review_task_id,
        "decision": outcome,
        "fields": corrected,
        "reviewer_id": reviewer,
        "source": "label_studio",
        "decided_at_unix": round(time.time(), 3),
        "decision_key": _decision_key(job_id, review_task_id, tenant_id),
    }
    stored = await asyncio.to_thread(_save_decision, job_id, review_task_id, decision)
    return {"status": "decided", **stored}


@app.post("/review/tasks/{job_id}/{review_task_id}/label-studio-event")
async def receive_label_studio_event(
    job_id: str,
    review_task_id: str,
    tenant_id: str,
    x_idp_review_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Treat events as hints; authoritative annotations come from Label Studio."""
    _require_decision_key(x_idp_review_key, tenant_id)
    return await poll_review_decision(
        job_id, review_task_id, tenant_id, settings.review_decision_api_key
    )
