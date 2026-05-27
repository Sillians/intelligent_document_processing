from __future__ import annotations

import logging
from typing import Any
import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shared.idp_common.config import get_settings
from shared.idp_common.storage import upload_json
settings = get_settings()
logger = logging.getLogger("workflow_orchestrator.activities")


def _is_non_retryable_status(status_code: int) -> bool:
    return 400 <= status_code < 500 and status_code not in {408, 429}


def _make_timeout() -> httpx.Timeout:
    connect_timeout = max(1, int(getattr(settings, "orchestrator_http_connect_timeout_seconds", 10)))
    request_timeout = max(connect_timeout, int(getattr(settings, "orchestrator_http_timeout_seconds", 300)))
    return httpx.Timeout(timeout=request_timeout, connect=connect_timeout)


def _is_ocr_fallback_error(error_message: str) -> bool:
    return (
        "network error calling" in error_message
        or "downstream HTTP 5" in error_message
        or "ReadTimeout" in error_message
        or "ConnectError" in error_message
    )


def _build_ocr_fallback_response(job_id: str, reason: str) -> dict[str, Any]:
    fallback_text = f"ocr_fallback:orchestrator:{reason[:96]}"
    payload = {
        "job_id": job_id,
        "tokens": [{"text": fallback_text, "bbox": [], "confidence": 0.2}],
        "token_count": 1,
        "mean_confidence": 0.2,
        "full_text": fallback_text,
        "engine_config": {},
        "fallback_used": True,
    }
    key = f"jobs/{job_id}/ocr/ocr.json"
    upload_json(settings, settings.ocr_bucket, key, payload)
    return {
        "job_id": job_id,
        "ocr_bucket": settings.ocr_bucket,
        "ocr_key": key,
        "token_count": 1,
        "mean_confidence": 0.2,
        "full_text": fallback_text,
        "fallback_used": True,
    }


async def _post(stage_name: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if isinstance(payload.get("job_id"), str):
        headers["X-Job-Id"] = payload["job_id"]

    try:
        async with httpx.AsyncClient(timeout=_make_timeout()) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        # Retryable by workflow activity retry policy.
        raise RuntimeError(f"{stage_name}: network error calling {url}: {exc}") from exc

    if response.status_code >= 400:
        body_preview = response.text[:500]
        message = f"{stage_name}: downstream HTTP {response.status_code} from {url}: {body_preview}"
        if _is_non_retryable_status(response.status_code):
            raise ApplicationError(
                message,
                type="DownstreamClientError",
                non_retryable=True,
            )
        raise RuntimeError(message)

    try:
        data = response.json()
    except ValueError as exc:
        raise ApplicationError(
            f"{stage_name}: response was not valid JSON",
            type="DownstreamContractError",
            non_retryable=True,
        ) from exc

    if not isinstance(data, dict):
        raise ApplicationError(
            f"{stage_name}: expected object JSON response",
            type="DownstreamContractError",
            non_retryable=True,
        )

    return data


@activity.defn
async def preprocess_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("preprocess", f"{settings.preprocess_url}/preprocess", payload)


@activity.defn
async def ocr_activity(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _post("ocr", f"{settings.ocr_url}/ocr", payload)
    except RuntimeError as exc:
        if not bool(getattr(settings, "orchestrator_enable_ocr_network_fallback", True)):
            raise

        message = str(exc)
        if not _is_ocr_fallback_error(message):
            raise

        job_id = str(payload.get("job_id") or "unknown")
        logger.warning(
            "OCR downstream unavailable; using orchestrator fallback artifact job_id=%s error=%s",
            job_id,
            message,
        )
        return _build_ocr_fallback_response(job_id, reason="downstream_unavailable")


@activity.defn
async def layout_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("layout", f"{settings.layout_url}/layout", payload)


@activity.defn
async def classify_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("classify", f"{settings.classifier_url}/route", payload)


@activity.defn
async def extract_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("extract", f"{settings.extraction_url}/extract", payload)


@activity.defn
async def validate_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("validate", f"{settings.validation_url}/validate", payload)


@activity.defn
async def create_review_task_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("review", f"{settings.review_url}/review/tasks", payload)


@activity.defn
async def deliver_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("deliver", f"{settings.delivery_url}/deliver", payload)


@activity.defn
async def evaluate_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("evaluate", f"{settings.evaluation_url}/track-run", payload)
