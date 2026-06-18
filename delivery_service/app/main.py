from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
import httpx

from delivery_service.app.models import DeliveryContext, DeliveryRequest, WebhookEventRequest
from delivery_service.app.providers import build_provider
from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_json, upload_json

settings = get_settings()
logger = logging.getLogger("delivery_service")
_DELIVERY_SEMAPHORE = asyncio.Semaphore(max(1, int(getattr(settings, "delivery_max_inflight_requests", 16))))


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="delivery-service", lifespan=lifespan)
instrument_app(app, "delivery-service")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _provider_names(request: DeliveryRequest) -> list[str]:
    raw_names = request.destinations
    if raw_names is None:
        raw_names = str(getattr(settings, "delivery_providers", "object_storage")).split(",")
    names = [str(name).strip().lower() for name in raw_names if str(name).strip()]
    return list(dict.fromkeys(names or ["object_storage"]))


def _delivery_id(request: DeliveryRequest, provider_names: list[str]) -> str:
    if request.idempotency_key:
        source = request.idempotency_key.strip()
    else:
        source = _stable_json(
            {
                "job_id": request.job_id,
                "payload": request.payload,
                "approval_status": request.approval_status,
                "destinations": provider_names,
                "webhook_url": request.webhook_url or "",
            }
        )
    return f"delivery-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]}"


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    redact_fields = {
        value.strip()
        for value in str(getattr(settings, "delivery_redact_fields", "")).split(",")
        if value.strip()
    }
    if not redact_fields:
        return dict(payload), []

    redacted: list[str] = []

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, nested in value.items():
                if str(key) in redact_fields:
                    output[str(key)] = "[REDACTED]"
                    redacted.append(str(key))
                else:
                    output[str(key)] = visit(nested)
            return output
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(payload), sorted(set(redacted))


def _validate_approval(request: DeliveryRequest) -> None:
    if not bool(getattr(settings, "delivery_require_approval", True)):
        return
    allowed = {
        value.strip().lower()
        for value in str(getattr(settings, "delivery_allowed_approval_statuses", "auto_approved,human_approved")).split(",")
        if value.strip()
    }
    approval_status = (request.approval_status or "").strip().lower()
    if approval_status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"delivery requires approval_status in: {','.join(sorted(allowed))}",
        )


def _receipt_key(job_id: str, delivery_id: str) -> str:
    return f"jobs/{job_id}/delivery/{delivery_id}/receipt.json"


def _webhook_receipt_key(job_id: str, event_id: str) -> str:
    return f"jobs/{job_id}/webhooks/{event_id}/receipt.json"


def _load_receipt(job_id: str, delivery_id: str) -> dict[str, Any] | None:
    key = _receipt_key(job_id, delivery_id)
    for bucket in (settings.delivery_bucket, settings.validation_bucket):
        try:
            return download_json(settings, bucket, key)
        except Exception:  # noqa: BLE001
            continue
    return None


def _persist_receipt(job_id: str, delivery_id: str, receipt: dict[str, Any]) -> tuple[str, str]:
    key = _receipt_key(job_id, delivery_id)
    bucket = settings.delivery_bucket
    try:
        upload_json(settings, bucket, key, receipt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("delivery receipt upload failed job_id=%s bucket=%s", job_id, bucket, exc_info=exc)
        bucket = settings.validation_bucket
        upload_json(settings, bucket, key, receipt)
    return bucket, key


def _load_webhook_receipt(job_id: str, event_id: str) -> dict[str, Any] | None:
    key = _webhook_receipt_key(job_id, event_id)
    for bucket in (settings.delivery_bucket, settings.validation_bucket):
        try:
            return download_json(settings, bucket, key)
        except Exception:  # noqa: BLE001
            continue
    return None


def _persist_webhook_receipt(job_id: str, event_id: str, receipt: dict[str, Any]) -> tuple[str, str]:
    key = _webhook_receipt_key(job_id, event_id)
    bucket = settings.delivery_bucket
    try:
        upload_json(settings, bucket, key, receipt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("webhook receipt upload failed job_id=%s bucket=%s", job_id, bucket, exc_info=exc)
        bucket = settings.validation_bucket
        upload_json(settings, bucket, key, receipt)
    return bucket, key


async def _deliver_with_retry(provider_name: str, context: DeliveryContext) -> dict[str, Any]:
    retry_max = max(0, int(getattr(settings, "delivery_retry_max", 2)))
    backoff = max(0.0, float(getattr(settings, "delivery_retry_backoff_seconds", 0.5)))
    provider = build_provider(provider_name)
    last_error: Exception | None = None

    for attempt in range(1, retry_max + 2):
        try:
            result = await provider.deliver(context)
            return {**result, "attempts": attempt}
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt > retry_max:
                break
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))

    assert last_error is not None
    raise last_error


def _webhook_url(request: WebhookEventRequest) -> str | None:
    configured_url = str(getattr(settings, "delivery_webhook_url", "")).strip()
    url = request.webhook_url or configured_url
    if not url:
        return None

    if request.webhook_url:
        if not bool(getattr(settings, "delivery_allow_request_webhook_url", False)):
            raise HTTPException(status_code=422, detail="per-request webhook_url is disabled")
        allowed_hosts = {
            value.strip().lower()
            for value in str(getattr(settings, "delivery_webhook_allowed_hosts", "")).split(",")
            if value.strip()
        }
        hostname = (urlparse(url).hostname or "").lower()
        if not allowed_hosts or hostname not in allowed_hosts:
            raise HTTPException(status_code=422, detail="webhook_url host is not allowed")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="webhook_url must be an absolute http(s) URL")
    return url


def _webhook_event_id(request: WebhookEventRequest) -> str:
    if request.event_id:
        return request.event_id.strip()
    if request.idempotency_key:
        source = request.idempotency_key.strip()
    else:
        source = _stable_json(
            {
                "event_type": request.event_type,
                "tenant_id": request.tenant_id,
                "job_id": request.job_id,
                "workflow_id": request.workflow_id or "",
                "data": request.data,
            }
        )
    return f"evt-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]}"


def _webhook_envelope(request: WebhookEventRequest, event_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": request.event_type,
        "occurred_at": request.occurred_at or datetime.now(UTC).isoformat(),
        "tenant_id": request.tenant_id,
        "job_id": request.job_id,
        "workflow_id": request.workflow_id,
        "data": request.data,
    }


def _webhook_headers(event_id: str, envelope: dict[str, Any], body: bytes) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": event_id,
        "X-IDP-Event-Id": event_id,
        "X-IDP-Event-Type": str(envelope["event_type"]),
        "X-IDP-Job-Id": str(envelope["job_id"]),
        "X-IDP-Tenant-Id": str(envelope["tenant_id"]),
    }

    secret = str(getattr(settings, "delivery_webhook_secret", ""))
    if bool(getattr(settings, "delivery_webhook_require_signature", True)) and not secret:
        raise HTTPException(status_code=422, detail="webhook delivery requires DELIVERY_WEBHOOK_SECRET")
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-IDP-Signature-256"] = f"sha256={signature}"
    return headers


async def _send_webhook_event(url: str, event_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    body = _stable_json(envelope).encode("utf-8")
    headers = _webhook_headers(event_id, envelope, body)

    retry_max = max(0, int(getattr(settings, "delivery_retry_max", 2)))
    backoff = max(0.0, float(getattr(settings, "delivery_retry_backoff_seconds", 0.5)))
    timeout = max(1, int(getattr(settings, "delivery_webhook_timeout_seconds", 15)))
    attempts: list[dict[str, Any]] = []
    last_error = ""

    for attempt in range(1, retry_max + 2):
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, content=body, headers=headers)
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            attempts.append(
                {
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "success": 200 <= response.status_code < 300,
                }
            )
            if 200 <= response.status_code < 300:
                return {"status": "success", "status_code": response.status_code, "attempts": attempts}
            last_error = response.text[:500]
        except Exception as exc:  # noqa: BLE001
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            last_error = str(exc)
            attempts.append({"attempt": attempt, "duration_ms": duration_ms, "success": False, "error": last_error[:500]})

        if attempt <= retry_max:
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))

    return {"status": "failed", "attempts": attempts, "error": last_error[:500]}


def _response_from_receipt(receipt: dict[str, Any], *, idempotent_replay: bool) -> dict[str, Any]:
    destination_results = receipt.get("destination_results", [])
    object_storage = next(
        (result for result in destination_results if result.get("provider") == "object_storage"),
        {},
    )
    webhook = next((result for result in destination_results if result.get("provider") == "webhook"), {})
    return {
        "job_id": receipt["job_id"],
        "delivery_id": receipt["delivery_id"],
        "delivery_artifact": object_storage.get("artifact", ""),
        "delivery_status": receipt["delivery_status"],
        "webhook_status": webhook.get("status", "skipped"),
        "delivered_at": receipt["delivered_at"],
        "delivery_bucket": receipt.get("delivery_bucket"),
        "delivery_receipt_key": receipt.get("delivery_receipt_key"),
        "destination_results": destination_results,
        "idempotent_replay": idempotent_replay,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "providers": _provider_names(DeliveryRequest(job_id="health", payload={}))}


@app.post("/deliver")
async def deliver(request: DeliveryRequest) -> dict[str, Any]:
    _validate_approval(request)
    provider_names = _provider_names(request)
    delivery_id = _delivery_id(request, provider_names)

    existing = await asyncio.to_thread(_load_receipt, request.job_id, delivery_id)
    if existing and existing.get("delivery_status") == "success":
        return _response_from_receipt(existing, idempotent_replay=True)

    try:
        await asyncio.wait_for(_DELIVERY_SEMAPHORE.acquire(), timeout=0.05)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="delivery worker is busy") from exc

    started = time.perf_counter()
    try:
        # Close the race between the initial replay check and acquiring capacity.
        existing = await asyncio.to_thread(_load_receipt, request.job_id, delivery_id)
        if existing and existing.get("delivery_status") == "success":
            return _response_from_receipt(existing, idempotent_replay=True)

        payload, redacted_fields = _redact_payload(request.payload)
        delivered_at = datetime.now(UTC).isoformat()
        envelope = {
            "schema_version": str(getattr(settings, "delivery_payload_format_version", "1.0")),
            "delivery_id": delivery_id,
            "job_id": request.job_id,
            "approval_status": request.approval_status,
            "payload": payload,
            "redacted_fields": redacted_fields,
            "source": {
                "extraction_bucket": request.extraction_bucket or "",
                "extraction_key": request.extraction_key or "",
                "validation_bucket": request.validation_bucket or "",
                "validation_key": request.validation_key or "",
            },
            "metadata": request.metadata,
            "delivered_at": delivered_at,
        }
        context = DeliveryContext(settings=settings, request=request, delivery_id=delivery_id, envelope=envelope)
        timeout = max(1, int(getattr(settings, "delivery_request_timeout_seconds", 60)))
        try:
            destination_results = await asyncio.wait_for(
                asyncio.gather(*[_deliver_with_retry(name, context) for name in provider_names]),
                timeout=timeout,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except asyncio.TimeoutError as exc:
            failed_receipt = {
                "job_id": request.job_id,
                "delivery_id": delivery_id,
                "delivery_status": "failed",
                "approval_status": request.approval_status,
                "providers": provider_names,
                "failure": "delivery request timed out",
                "delivered_at": delivered_at,
            }
            await asyncio.to_thread(_persist_receipt, request.job_id, delivery_id, failed_receipt)
            raise HTTPException(status_code=504, detail="delivery request timed out") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("delivery provider failed job_id=%s delivery_id=%s", request.job_id, delivery_id, exc_info=exc)
            failed_receipt = {
                "job_id": request.job_id,
                "delivery_id": delivery_id,
                "delivery_status": "failed",
                "approval_status": request.approval_status,
                "providers": provider_names,
                "failure": str(exc)[:500],
                "delivered_at": delivered_at,
            }
            await asyncio.to_thread(_persist_receipt, request.job_id, delivery_id, failed_receipt)
            raise HTTPException(status_code=502, detail=f"delivery provider failed: {exc}") from exc

        receipt = {
            "job_id": request.job_id,
            "delivery_id": delivery_id,
            "delivery_status": "success",
            "approval_status": request.approval_status,
            "providers": provider_names,
            "destination_results": destination_results,
            "redacted_fields": redacted_fields,
            "delivered_at": delivered_at,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        receipt_bucket, receipt_key = await asyncio.to_thread(_persist_receipt, request.job_id, delivery_id, receipt)
        receipt["delivery_bucket"] = receipt_bucket
        receipt["delivery_receipt_key"] = receipt_key
        # Persist receipt again with its final location included.
        await asyncio.to_thread(upload_json, settings, receipt_bucket, receipt_key, receipt)
        return _response_from_receipt(receipt, idempotent_replay=False)
    finally:
        _DELIVERY_SEMAPHORE.release()


@app.post("/webhooks/events")
async def deliver_webhook_event(request: WebhookEventRequest) -> dict[str, Any]:
    event_id = _webhook_event_id(request)
    existing = await asyncio.to_thread(_load_webhook_receipt, request.job_id, event_id)
    if existing and existing.get("webhook_status") == "success":
        return {
            "event_id": event_id,
            "job_id": request.job_id,
            "webhook_status": existing["webhook_status"],
            "webhook_receipt_key": existing.get("webhook_receipt_key"),
            "idempotent_replay": True,
        }

    envelope = _webhook_envelope(request, event_id)
    url = _webhook_url(request)
    if url is None:
        receipt = {
            "event_id": event_id,
            "event_type": request.event_type,
            "job_id": request.job_id,
            "tenant_id": request.tenant_id,
            "workflow_id": request.workflow_id,
            "webhook_status": "skipped",
            "skip_reason": "webhook destination is not configured",
            "sent_at": datetime.now(UTC).isoformat(),
            "envelope": envelope,
        }
        receipt_bucket, receipt_key = await asyncio.to_thread(_persist_webhook_receipt, request.job_id, event_id, receipt)
        receipt["webhook_bucket"] = receipt_bucket
        receipt["webhook_receipt_key"] = receipt_key
        await asyncio.to_thread(upload_json, settings, receipt_bucket, receipt_key, receipt)
        return {
            "event_id": event_id,
            "job_id": request.job_id,
            "webhook_status": "skipped",
            "webhook_bucket": receipt_bucket,
            "webhook_receipt_key": receipt_key,
            "idempotent_replay": False,
        }

    sent_at = datetime.now(UTC).isoformat()
    try:
        result = await _send_webhook_event(url, event_id, envelope)
    except HTTPException as exc:
        receipt = {
            "event_id": event_id,
            "event_type": request.event_type,
            "job_id": request.job_id,
            "tenant_id": request.tenant_id,
            "workflow_id": request.workflow_id,
            "webhook_status": "failed",
            "destination_url": url,
            "attempts": [],
            "status_code": exc.status_code,
            "error": str(exc.detail)[:500],
            "sent_at": sent_at,
            "envelope": envelope,
        }
        receipt_bucket, receipt_key = await asyncio.to_thread(_persist_webhook_receipt, request.job_id, event_id, receipt)
        receipt["webhook_bucket"] = receipt_bucket
        receipt["webhook_receipt_key"] = receipt_key
        await asyncio.to_thread(upload_json, settings, receipt_bucket, receipt_key, receipt)
        raise

    receipt = {
        "event_id": event_id,
        "event_type": request.event_type,
        "job_id": request.job_id,
        "tenant_id": request.tenant_id,
        "workflow_id": request.workflow_id,
        "webhook_status": result["status"],
        "destination_url": url,
        "attempts": result.get("attempts", []),
        "status_code": result.get("status_code"),
        "error": result.get("error"),
        "sent_at": sent_at,
        "envelope": envelope,
    }
    receipt_bucket, receipt_key = await asyncio.to_thread(_persist_webhook_receipt, request.job_id, event_id, receipt)
    receipt["webhook_bucket"] = receipt_bucket
    receipt["webhook_receipt_key"] = receipt_key
    await asyncio.to_thread(upload_json, settings, receipt_bucket, receipt_key, receipt)

    if result["status"] != "success":
        raise HTTPException(status_code=502, detail="webhook delivery failed")

    return {
        "event_id": event_id,
        "job_id": request.job_id,
        "webhook_status": result["status"],
        "webhook_bucket": receipt_bucket,
        "webhook_receipt_key": receipt_key,
        "idempotent_replay": False,
    }


@app.get("/deliveries/{job_id}/{delivery_id}")
async def get_delivery(job_id: str, delivery_id: str) -> dict[str, Any]:
    receipt = await asyncio.to_thread(_load_receipt, job_id, delivery_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="delivery receipt not found")
    return receipt
