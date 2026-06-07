from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import re
import time
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_json, upload_json

settings = get_settings()
logger = logging.getLogger("validation_service")
_VALIDATION_MAX_INFLIGHT = max(1, int(getattr(settings, "validation_max_inflight_requests", 16)))
_VALIDATION_POOL = ThreadPoolExecutor(max_workers=_VALIDATION_MAX_INFLIGHT, thread_name_prefix="validation-worker")
_VALIDATION_INFLIGHT = 0
_VALIDATION_INFLIGHT_LOCK = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _VALIDATION_POOL.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="validation-service", lifespan=lifespan)
instrument_app(app, "validation-service")


class ValidationRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    extraction_key: str = Field(min_length=1, max_length=1024)
    extraction_bucket: str | None = Field(default=None, min_length=1, max_length=128)
    fields: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    used_vlm_fallback: bool
    route: str | None = Field(default=None, max_length=64)


@dataclass(frozen=True)
class ValidationRuleSet:
    required_fields: tuple[str, ...]
    recommended_fields: tuple[str, ...] = ()
    date_fields: tuple[str, ...] = ()
    money_fields: tuple[str, ...] = ()
    allow_vlm_auto_approval: bool = False


RULESETS: dict[str, ValidationRuleSet] = {
    "invoice": ValidationRuleSet(
        required_fields=("invoice_number", "invoice_date", "total_amount"),
        recommended_fields=("currency", "vendor_name"),
        date_fields=("invoice_date",),
        money_fields=("total_amount",),
    ),
    "receipt": ValidationRuleSet(
        required_fields=("receipt_date", "merchant_name", "total_amount"),
        recommended_fields=("tax_amount", "payment_method"),
        date_fields=("receipt_date",),
        money_fields=("total_amount", "tax_amount"),
    ),
    "contract": ValidationRuleSet(
        required_fields=("parties", "effective_date"),
        recommended_fields=("term", "governing_law", "signature_date"),
        date_fields=("effective_date", "signature_date"),
    ),
    "purchase_order": ValidationRuleSet(
        required_fields=("purchase_order_number", "supplier_name", "total_amount"),
        recommended_fields=("order_date", "buyer_name"),
        date_fields=("order_date",),
        money_fields=("total_amount",),
    ),
    "bank_statement": ValidationRuleSet(
        required_fields=("account_number", "statement_period", "closing_balance"),
        recommended_fields=("opening_balance", "transactions"),
        money_fields=("opening_balance", "closing_balance"),
    ),
    "generic": ValidationRuleSet(
        required_fields=("summary",),
        recommended_fields=("document_date", "primary_party", "secondary_party", "total_amount"),
        date_fields=("document_date",),
        money_fields=("total_amount",),
        allow_vlm_auto_approval=True,
    ),
}

ROUTE_HINTS: tuple[tuple[str, set[str]], ...] = (
    ("invoice", {"invoice_number", "invoice_date"}),
    ("receipt", {"receipt_date", "merchant_name"}),
    ("contract", {"effective_date", "parties", "governing_law"}),
    ("purchase_order", {"purchase_order_number", "supplier_name", "buyer_name"}),
    ("bank_statement", {"account_number", "statement_period", "closing_balance"}),
)

DATE_PATTERN = re.compile(
    r"^(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})$"
)
MONEY_PATTERN = re.compile(r"^[A-Z]{3}\s+[-+]?\d[\d,]*(?:\.\d{2})?$|^[$€£₦]?\s*[-+]?\d[\d,]*(?:\.\d{2})?$")


def _try_acquire_request_slot() -> bool:
    global _VALIDATION_INFLIGHT
    with _VALIDATION_INFLIGHT_LOCK:
        if _VALIDATION_INFLIGHT >= _VALIDATION_MAX_INFLIGHT:
            return False
        _VALIDATION_INFLIGHT += 1
        return True


def _release_request_slot(_: Future[Any] | None = None) -> None:
    global _VALIDATION_INFLIGHT
    with _VALIDATION_INFLIGHT_LOCK:
        _VALIDATION_INFLIGHT = max(0, _VALIDATION_INFLIGHT - 1)


def _normalize_route(route: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", (route or "").lower()).strip("_")
    return normalized if normalized in RULESETS else "generic"


def _clean_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(_clean_value(item) for item in value if _clean_value(item))
    if isinstance(value, dict):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_fields(fields: dict[str, Any]) -> dict[str, str]:
    return {str(key): _clean_value(value) for key, value in fields.items()}


def infer_route(fields: dict[str, str], explicit_route: str | None = None) -> str:
    route = _normalize_route(explicit_route)
    if route != "generic":
        return route

    populated = {key for key, value in fields.items() if value}
    for candidate, hints in ROUTE_HINTS:
        if populated & hints:
            return candidate
    return "generic"


def _is_present(fields: dict[str, str], field: str) -> bool:
    return bool(fields.get(field, "").strip())


def _validate_date(value: str) -> bool:
    value = value.strip()
    return not value or bool(DATE_PATTERN.match(value))


def _validate_money(value: str) -> bool:
    value = value.strip()
    return not value or bool(MONEY_PATTERN.match(value))


def _field_confidence(field_confidences: dict[str, Any], field: str, default: float) -> float:
    value = field_confidences.get(field, default)
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def validate_payload(
    *,
    job_id: str,
    fields: dict[str, Any],
    confidence: float,
    used_vlm_fallback: bool,
    route: str | None = None,
    field_confidences: dict[str, Any] | None = None,
    extraction_key: str = "",
    artifact_loaded: bool = False,
) -> dict[str, Any]:
    normalized_fields = normalize_fields(fields)
    resolved_route = infer_route(normalized_fields, route)
    ruleset = RULESETS[resolved_route]
    field_confidences = field_confidences or {}
    reasons: list[str] = []
    warnings: list[str] = []
    rule_results: list[dict[str, Any]] = []

    missing_required = [field for field in ruleset.required_fields if not _is_present(normalized_fields, field)]
    if missing_required:
        reasons.append(f"missing_required_fields:{','.join(missing_required)}")
    rule_results.append(
        {
            "rule": "required_fields_present",
            "status": "failed" if missing_required else "passed",
            "fields": list(ruleset.required_fields),
            "failed_fields": missing_required,
        }
    )

    missing_recommended = [field for field in ruleset.recommended_fields if not _is_present(normalized_fields, field)]
    if missing_recommended:
        warnings.append(f"missing_recommended_fields:{','.join(missing_recommended)}")
    rule_results.append(
        {
            "rule": "recommended_fields_present",
            "status": "warning" if missing_recommended else "passed",
            "fields": list(ruleset.recommended_fields),
            "failed_fields": missing_recommended,
        }
    )

    invalid_dates = [field for field in ruleset.date_fields if not _validate_date(normalized_fields.get(field, ""))]
    if invalid_dates:
        reasons.append(f"invalid_date_format:{','.join(invalid_dates)}")
    rule_results.append(
        {
            "rule": "date_format",
            "status": "failed" if invalid_dates else "passed",
            "fields": list(ruleset.date_fields),
            "failed_fields": invalid_dates,
        }
    )

    invalid_money = [field for field in ruleset.money_fields if not _validate_money(normalized_fields.get(field, ""))]
    if invalid_money:
        reasons.append(f"invalid_money_format:{','.join(invalid_money)}")
    rule_results.append(
        {
            "rule": "money_format",
            "status": "failed" if invalid_money else "passed",
            "fields": list(ruleset.money_fields),
            "failed_fields": invalid_money,
        }
    )

    min_field_confidence = float(getattr(settings, "validation_min_field_confidence", 0.60))
    low_confidence_fields = [
        field
        for field in ruleset.required_fields
        if _is_present(normalized_fields, field)
        and _field_confidence(field_confidences, field, confidence) < min_field_confidence
    ]
    if low_confidence_fields:
        reasons.append(f"low_field_confidence:{','.join(low_confidence_fields)}")
    rule_results.append(
        {
            "rule": "required_field_confidence",
            "status": "failed" if low_confidence_fields else "passed",
            "threshold": min_field_confidence,
            "failed_fields": low_confidence_fields,
        }
    )

    if confidence < settings.auto_approve_threshold:
        reasons.append(f"low_confidence:{confidence:.3f}")
    rule_results.append(
        {
            "rule": "document_confidence",
            "status": "failed" if confidence < settings.auto_approve_threshold else "passed",
            "threshold": settings.auto_approve_threshold,
            "actual": round(confidence, 4),
        }
    )

    if used_vlm_fallback and not ruleset.allow_vlm_auto_approval:
        reasons.append("llm_fallback_requires_review")
    rule_results.append(
        {
            "rule": "llm_fallback_policy",
            "status": "failed" if used_vlm_fallback and not ruleset.allow_vlm_auto_approval else "passed",
            "used_vlm_fallback": used_vlm_fallback,
        }
    )

    populated_count = sum(1 for value in normalized_fields.values() if value)
    auto_reject_threshold = float(getattr(settings, "validation_auto_reject_threshold", 0.25))
    reject = populated_count == 0 or (confidence < auto_reject_threshold and missing_required)
    if reject:
        reasons.append("reject_unusable_extraction")

    requires_human_review = bool(reasons)
    verdict = "rejected" if reject else "needs_review" if requires_human_review else "auto_approved"

    return {
        "job_id": job_id,
        "verdict": verdict,
        "requires_human_review": requires_human_review,
        "reasons": reasons,
        "warnings": warnings,
        "route": resolved_route,
        "confidence": round(confidence, 4),
        "used_vlm_fallback": used_vlm_fallback,
        "fields": normalized_fields,
        "rule_results": rule_results,
        "validation_profile": getattr(settings, "validation_profile", "default"),
        "policy_version": getattr(settings, "validation_policy_version", "2026-06-03"),
        "source": {
            "extraction_key": extraction_key,
            "artifact_loaded": artifact_loaded,
        },
    }


def _load_extraction_artifact(request: ValidationRequest) -> tuple[dict[str, Any], bool]:
    bucket = request.extraction_bucket
    if not bucket:
        return {}, False
    try:
        artifact = download_json(settings, bucket, request.extraction_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "validation could not load extraction artifact job_id=%s bucket=%s key=%s error=%s",
            request.job_id,
            bucket,
            request.extraction_key,
            exc,
        )
        return {}, False
    return artifact, True


def _persist_payload(job_id: str, payload: dict[str, Any]) -> tuple[str, str]:
    key = f"jobs/{job_id}/validation/result.json"
    validation_bucket = getattr(settings, "validation_bucket", "validation-artifacts")
    try:
        upload_json(settings, validation_bucket, key, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "primary validation artifact upload failed job_id=%s bucket=%s",
            job_id,
            validation_bucket,
            exc_info=exc,
        )
        fallback_bucket = settings.extraction_bucket
        if fallback_bucket == validation_bucket:
            raise
        upload_json(settings, fallback_bucket, key, payload)
        validation_bucket = fallback_bucket
        logger.warning("validation artifact upload fell back job_id=%s fallback_bucket=%s", job_id, fallback_bucket)
    return validation_bucket, key


def _validate_sync(request: ValidationRequest) -> dict[str, Any]:
    started = time.perf_counter()
    artifact, artifact_loaded = _load_extraction_artifact(request)
    fields = artifact.get("fields") if isinstance(artifact.get("fields"), dict) else request.fields
    route = request.route or artifact.get("route")
    confidence = float(artifact.get("confidence", request.confidence))
    used_vlm_fallback = bool(artifact.get("used_vlm_fallback", request.used_vlm_fallback))
    field_confidences = artifact.get("field_confidences") if isinstance(artifact.get("field_confidences"), dict) else {}

    payload = validate_payload(
        job_id=request.job_id,
        fields=fields,
        confidence=confidence,
        used_vlm_fallback=used_vlm_fallback,
        route=route if isinstance(route, str) else None,
        field_confidences=field_confidences,
        extraction_key=request.extraction_key,
        artifact_loaded=artifact_loaded,
    )
    payload["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    if bool(getattr(settings, "validation_persist_decision", True)):
        bucket, key = _persist_payload(request.job_id, payload)
        payload["validation_bucket"] = bucket
        payload["validation_key"] = key
    return payload


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validate")
async def validate(request: ValidationRequest) -> dict[str, Any]:
    if not _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="validation worker is busy")

    future: Future[dict[str, Any]] = _VALIDATION_POOL.submit(_validate_sync, request)
    future.add_done_callback(_release_request_slot)
    timeout = max(1, int(getattr(settings, "validation_request_timeout_seconds", 30)))
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="validation request timed out") from exc
