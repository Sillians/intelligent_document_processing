from __future__ import annotations

import logging
from typing import Any
import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shared.idp_common.config import get_settings
settings = get_settings()
logger = logging.getLogger("workflow_orchestrator.activities")


def _is_non_retryable_status(status_code: int) -> bool:
    return 400 <= status_code < 500 and status_code not in {408, 429}


def _make_timeout(stage_name: str = "", payload: dict[str, Any] | None = None) -> httpx.Timeout:
    connect_timeout = max(1, int(getattr(settings, "orchestrator_http_connect_timeout_seconds", 10)))
    request_timeout = max(connect_timeout, int(getattr(settings, "orchestrator_http_timeout_seconds", 300)))
    if stage_name == "preprocess":
        request_timeout = max(request_timeout, settings.preprocess_request_timeout_seconds * settings.preprocess_max_pages + 30)
    elif stage_name in {"ocr", "layout"}:
        page_count = len((payload or {}).get("preprocessed_pages") or [1])
        per_page = settings.ocr_request_timeout_seconds if stage_name == "ocr" else settings.layout_request_timeout_seconds
        request_timeout = max(request_timeout, per_page * page_count + 30)
    return httpx.Timeout(timeout=request_timeout, connect=connect_timeout)


async def _post(stage_name: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if isinstance(payload.get("job_id"), str):
        headers["X-Job-Id"] = payload["job_id"]
    if settings.idp_internal_api_key:
        headers["X-IDP-Internal-Key"] = settings.idp_internal_api_key

    try:
        async with httpx.AsyncClient(timeout=_make_timeout(stage_name, payload)) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        # Retryable by workflow activity retry policy.
        raise RuntimeError(f"{stage_name}: network error calling {url}: {exc}") from exc

    if response.status_code >= 400:
        if stage_name == "ocr":
            try:
                failure = response.json().get("detail")
            except (ValueError, AttributeError):
                failure = None
            if isinstance(failure, dict) and failure.get("schema_version") == "1.0" and failure.get("stage") == "ocr" and isinstance(failure.get("retryable"), bool) and str(failure.get("code", "")).startswith("OCR_"):
                raise ApplicationError(
                    failure["code"], failure, type=failure["code"],
                    non_retryable=not failure["retryable"],
                )
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
async def poll_review_activity(payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.review_decision_api_key:
        raise ApplicationError("REVIEW_DECISION_API_KEY is required for review completion", non_retryable=True)
    url = f"{settings.review_url}/review/tasks/{payload['job_id']}/{payload['review_task_id']}/decision"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.get(
                url,
                params={"tenant_id": payload["tenant_id"]},
                headers={"X-IDP-Review-Key": settings.review_decision_api_key},
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("status") not in {"pending", "decided"}:
            raise ApplicationError("Review poll returned invalid response", non_retryable=True)
        return data
    except httpx.HTTPStatusError as exc:
        if _is_non_retryable_status(exc.response.status_code):
            raise ApplicationError("Review poll rejected", non_retryable=True) from exc
        logger.warning("review provider temporarily unavailable job_id=%s", payload["job_id"])
        return {"status": "pending", "provider_unavailable": True}
    except httpx.RequestError:
        logger.warning("review provider temporarily unreachable job_id=%s", payload["job_id"])
        return {"status": "pending", "provider_unavailable": True}


@activity.defn
async def preprocess_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("preprocess", f"{settings.preprocess_url}/preprocess", payload)


@activity.defn
async def ocr_activity(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _post("ocr", f"{settings.ocr_url}/ocr", payload)
    except RuntimeError as exc:
        raise ApplicationError("OCR_UNAVAILABLE", type="OCR_UNAVAILABLE") from exc


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
async def poll_delivery_activity(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{settings.delivery_url}/deliveries/{payload['job_id']}/{payload['delivery_id']}",
                params={"tenant_id": payload["tenant_id"]}, headers={"X-IDP-Internal-Key": settings.idp_internal_api_key})
        response.raise_for_status()
        result = response.json()
        if result.get('job_id') != payload['job_id'] or result.get('tenant_id') != payload['tenant_id']:
            raise ApplicationError('Delivery receipt scope mismatch', non_retryable=True)
        return result
    except httpx.HTTPError:
        return {**payload, 'delivery_status': 'pending', 'provider_unavailable': True}


@activity.defn
async def record_ocr_exception_activity(payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.review_url}/review/ocr-exceptions", json=payload,
                                     headers={"X-IDP-Review-Key": settings.review_decision_api_key})
    response.raise_for_status()
    return response.json()


@activity.defn
async def evaluate_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("evaluate", f"{settings.evaluation_url}/track-run", payload)


@activity.defn
async def notify_webhook_activity(payload: dict[str, Any]) -> dict[str, Any]:
    return await _post("webhook", f"{settings.delivery_url}/webhooks/events", payload)
