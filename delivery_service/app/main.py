from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException

from delivery_service.app.models import DeliveryContext, DeliveryRequest
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


@app.get("/deliveries/{job_id}/{delivery_id}")
async def get_delivery(job_id: str, delivery_id: str) -> dict[str, Any]:
    receipt = await asyncio.to_thread(_load_receipt, job_id, delivery_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="delivery receipt not found")
    return receipt
