from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import logging
import math
import time
from typing import Any

from fastapi import FastAPI, HTTPException

from evaluation_service.app.models import EvaluationContext, EvaluationRequest
from evaluation_service.app.providers import build_provider
from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_json, upload_json

settings = get_settings()
logger = logging.getLogger("evaluation_service")
_EVALUATION_SEMAPHORE = asyncio.Semaphore(max(1, int(getattr(settings, "evaluation_max_inflight_requests", 16))))


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="evaluation-service", lifespan=lifespan)
instrument_app(app, "evaluation-service")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _provider_names() -> list[str]:
    raw = str(getattr(settings, "evaluation_providers", "mlflow,artifact_store"))
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    return list(dict.fromkeys(names or ["artifact_store"]))


def _evaluation_id(request: EvaluationRequest) -> str:
    source = request.idempotency_key or _stable_json(request.model_dump(exclude={"idempotency_key"}))
    return f"evaluation-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]}"


def _safe_metric(value: Any, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def build_metrics(request: EvaluationRequest) -> dict[str, float]:
    ocr_confidence = _safe_metric(request.ocr_confidence)
    extraction_confidence = _safe_metric(request.extraction_confidence)
    metrics = {
        "ocr_confidence": ocr_confidence,
        "extraction_confidence": extraction_confidence,
        "confidence_mean": round((ocr_confidence + extraction_confidence) / 2, 6),
        "confidence_gap": round(abs(ocr_confidence - extraction_confidence), 6),
        "used_vlm_fallback": float(request.used_vlm_fallback),
        "requires_human_review": float(request.requires_human_review),
        "outcome_delivered": float(request.status == "delivered"),
        "outcome_pending_human_review": float(request.status == "pending_human_review"),
        "outcome_failed": float(request.status == "failed"),
    }
    if request.field_count is not None:
        metrics["field_count"] = float(request.field_count)
    if request.populated_field_count is not None:
        metrics["populated_field_count"] = float(request.populated_field_count)
    if request.field_count and request.populated_field_count is not None:
        metrics["field_completeness"] = round(min(1.0, request.populated_field_count / request.field_count), 6)
    for name, value in request.custom_metrics.items():
        metrics[name] = _safe_metric(value)
    return metrics


def _stringify_parameters(parameters: dict[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, value in parameters.items():
        if isinstance(value, (dict, list, tuple)):
            output[str(key)] = _stable_json(value)[:500]
        else:
            output[str(key)] = str(value)[:500]
    return output


def build_parameters(request: EvaluationRequest) -> dict[str, str]:
    parameters = {
        "job_id": request.job_id,
        "status": request.status,
        "route": request.route or "unknown",
        "validation_verdict": request.validation_verdict or "unknown",
        "used_vlm_fallback": str(request.used_vlm_fallback),
        "requires_human_review": str(request.requires_human_review),
        "dataset_version": request.dataset_version or str(getattr(settings, "evaluation_dataset_version", "production")),
        "pipeline_version": request.pipeline_version or str(getattr(settings, "evaluation_pipeline_version", "development")),
    }
    parameters.update(_stringify_parameters(request.parameters))
    return parameters


def build_tags(request: EvaluationRequest, evaluation_id: str) -> dict[str, str]:
    tags = {
        "evaluation_id": evaluation_id,
        "service": "evaluation-service",
        "pipeline_status": request.status,
        "route": request.route or "unknown",
    }
    tags.update({str(key): str(value)[:500] for key, value in request.tags.items()})
    return tags


def _receipt_key(job_id: str, evaluation_id: str) -> str:
    return f"jobs/{job_id}/evaluation/{evaluation_id}/receipt.json"


def _load_receipt(job_id: str, evaluation_id: str) -> dict[str, Any] | None:
    key = _receipt_key(job_id, evaluation_id)
    for bucket in (settings.evaluation_bucket, settings.delivery_bucket):
        try:
            return download_json(settings, bucket, key)
        except Exception:  # noqa: BLE001
            continue
    return None


def _persist_receipt(job_id: str, evaluation_id: str, receipt: dict[str, Any]) -> tuple[str, str]:
    key = _receipt_key(job_id, evaluation_id)
    bucket = settings.evaluation_bucket
    try:
        upload_json(settings, bucket, key, receipt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("evaluation receipt upload failed job_id=%s bucket=%s", job_id, bucket, exc_info=exc)
        bucket = settings.delivery_bucket
        upload_json(settings, bucket, key, receipt)
    return bucket, key


def _mlflow_run_id(provider_results: list[dict[str, Any]]) -> str:
    result = next((item for item in provider_results if item.get("provider") == "mlflow" and item.get("status") == "success"), {})
    return str(result.get("run_id") or "")


def _response_from_receipt(receipt: dict[str, Any], *, idempotent_replay: bool) -> dict[str, Any]:
    provider_results = receipt.get("provider_results", [])
    return {
        "job_id": receipt["job_id"],
        "evaluation_id": receipt["evaluation_id"],
        "mlflow_run_id": _mlflow_run_id(provider_results),
        "tracked": receipt.get("tracking_status") in {"success", "partial_success"},
        "tracking_status": receipt.get("tracking_status", "unknown"),
        "provider_results": provider_results,
        "evaluation_bucket": receipt.get("evaluation_bucket"),
        "evaluation_receipt_key": receipt.get("evaluation_receipt_key"),
        "idempotent_replay": idempotent_replay,
    }


async def _track_provider(name: str, context: EvaluationContext) -> dict[str, Any]:
    provider = build_provider(name)
    retry_max = max(0, int(getattr(settings, "evaluation_retry_max", 1)))
    backoff = max(0.0, float(getattr(settings, "evaluation_retry_backoff_seconds", 0.5)))
    last_error: Exception | None = None
    for attempt in range(1, retry_max + 2):
        try:
            result = await provider.track(context)
            return {**result, "attempts": attempt}
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt > retry_max:
                break
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
    assert last_error is not None
    return {
        "provider": name,
        "status": "failed",
        "attempts": retry_max + 1,
        "error": str(last_error)[:500],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "providers": _provider_names()}


@app.post("/track-run")
async def track_run(request: EvaluationRequest) -> dict[str, Any]:
    evaluation_id = _evaluation_id(request)
    existing = await asyncio.to_thread(_load_receipt, request.job_id, evaluation_id)
    if existing and existing.get("tracking_status") in {"success", "partial_success"}:
        return _response_from_receipt(existing, idempotent_replay=True)

    try:
        await asyncio.wait_for(_EVALUATION_SEMAPHORE.acquire(), timeout=0.05)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="evaluation worker is busy") from exc

    started = time.perf_counter()
    try:
        provider_names = _provider_names()
        metrics = build_metrics(request)
        parameters = build_parameters(request)
        tags = build_tags(request, evaluation_id)
        context = EvaluationContext(
            settings=settings,
            request=request,
            evaluation_id=evaluation_id,
            metrics=metrics,
            parameters=parameters,
            tags=tags,
        )
        timeout = max(1, int(getattr(settings, "evaluation_request_timeout_seconds", 60)))
        try:
            provider_results = await asyncio.wait_for(
                asyncio.gather(*[_track_provider(name, context) for name in provider_names]),
                timeout=timeout,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="evaluation request timed out") from exc

        success_count = sum(1 for result in provider_results if result.get("status") == "success")
        if success_count == len(provider_results):
            tracking_status = "success"
        elif success_count > 0:
            tracking_status = "partial_success"
        else:
            tracking_status = "failed"

        receipt = {
            "job_id": request.job_id,
            "evaluation_id": evaluation_id,
            "tracking_status": tracking_status,
            "provider_results": provider_results,
            "metrics": metrics,
            "parameters": parameters,
            "tags": tags,
            "tracked_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        bucket, key = await asyncio.to_thread(_persist_receipt, request.job_id, evaluation_id, receipt)
        receipt["evaluation_bucket"] = bucket
        receipt["evaluation_receipt_key"] = key
        await asyncio.to_thread(upload_json, settings, bucket, key, receipt)

        if tracking_status == "failed" and bool(getattr(settings, "evaluation_fail_when_all_providers_fail", False)):
            raise HTTPException(status_code=502, detail="all evaluation providers failed")
        return _response_from_receipt(receipt, idempotent_replay=False)
    finally:
        _EVALUATION_SEMAPHORE.release()


@app.get("/evaluations/{job_id}/{evaluation_id}")
async def get_evaluation(job_id: str, evaluation_id: str) -> dict[str, Any]:
    receipt = await asyncio.to_thread(_load_receipt, job_id, evaluation_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="evaluation receipt not found")
    return receipt
