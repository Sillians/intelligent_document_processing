from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import logging
import re
import time
from statistics import mean
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_json, upload_json

settings = get_settings()
logger = logging.getLogger("extraction_service")
_EXTRACTION_MAX_INFLIGHT = max(1, int(getattr(settings, "extraction_max_inflight_requests", 4)))
_EXTRACTION_POOL = ThreadPoolExecutor(max_workers=_EXTRACTION_MAX_INFLIGHT, thread_name_prefix="extraction-worker")
_EXTRACTION_INFLIGHT = 0
_EXTRACTION_INFLIGHT_LOCK = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _EXTRACTION_POOL.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="extraction-service", lifespan=lifespan)
instrument_app(app, "extraction-service")


class ExtractRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    ocr_key: str = Field(min_length=1, max_length=1024)
    layout_key: str = Field(min_length=1, max_length=1024)
    ocr_confidence: float = Field(ge=0.0, le=1.0)
    route: str = Field(min_length=1, max_length=64)
    strategy_profile: str | None = Field(default=None, max_length=128)
    extraction_mode: str | None = Field(default=None, max_length=128)


SCHEMAS: dict[str, list[str]] = {
    "invoice": ["invoice_number", "invoice_date", "total_amount", "currency", "vendor_name"],
    "receipt": ["receipt_date", "merchant_name", "total_amount", "tax_amount", "payment_method"],
    "contract": ["effective_date", "parties", "term", "governing_law", "signature_date"],
    "purchase_order": ["purchase_order_number", "order_date", "supplier_name", "buyer_name", "total_amount"],
    "bank_statement": ["account_number", "statement_period", "opening_balance", "closing_balance", "transactions"],
    "generic": ["document_date", "total_amount", "primary_party", "secondary_party", "summary"],
}


def _try_acquire_request_slot() -> bool:
    global _EXTRACTION_INFLIGHT
    with _EXTRACTION_INFLIGHT_LOCK:
        if _EXTRACTION_INFLIGHT >= _EXTRACTION_MAX_INFLIGHT:
            return False
        _EXTRACTION_INFLIGHT += 1
        return True


def _release_request_slot(_: Future[Any] | None = None) -> None:
    global _EXTRACTION_INFLIGHT
    with _EXTRACTION_INFLIGHT_LOCK:
        _EXTRACTION_INFLIGHT = max(0, _EXTRACTION_INFLIGHT - 1)


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _first_group(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return _normalize_space(match.group(1)) if match else ""


def _all_groups(pattern: str, text: str, *, limit: int = 10) -> list[str]:
    values: list[str] = []
    for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
        value = _normalize_space(match.group(1))
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _normalize_route(route: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", route.lower()).strip("_")
    return normalized if normalized in SCHEMAS else "generic"


def _extract_text_from_ocr(ocr: dict[str, Any]) -> tuple[str, float, bool]:
    tokens = [token for token in ocr.get("tokens", []) if isinstance(token, dict)]
    text = "\n".join(str(token.get("text") or "").strip() for token in tokens if str(token.get("text") or "").strip())
    if not text:
        text = str(ocr.get("full_text") or "").strip()
    confidences = [
        float(token.get("confidence"))
        for token in tokens
        if isinstance(token.get("confidence"), (int, float))
    ]
    mean_confidence = round(mean(confidences), 4) if confidences else float(ocr.get("mean_confidence") or 0.0)
    fallback_used = bool(ocr.get("fallback_used")) or "ocr_fallback:" in text.lower()
    return text, mean_confidence, fallback_used


def _extract_text_from_layout(layout: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    blocks = [block for block in layout.get("blocks", []) if isinstance(block, dict)]
    text_blocks = [block for block in blocks if str(block.get("text") or "").strip()]
    ordered = sorted(text_blocks, key=lambda block: int(block.get("reading_order", 0)))
    text = "\n".join(_normalize_space(str(block.get("text") or "")) for block in ordered)
    return text.strip(), ordered


def _money_pattern(label: str) -> str:
    return rf"(?:{label})\s*[:\-]?\s*([$€£]?\s?[0-9,]+(?:\.[0-9]{{2}})?)"


def _date_pattern(label: str) -> str:
    return rf"(?:{label})\s*[:\-]?\s*(\d{{1,4}}[\-/]\d{{1,2}}[\-/]\d{{1,4}}|[A-Za-z]{{3,9}}\s+\d{{1,2}},?\s+\d{{4}})"


def _label_value_pattern(label: str, stop_labels: str) -> str:
    return rf"(?:{label})\s*[:\-]?\s*([A-Za-z0-9 .,&'/-]{{3,80}}?)(?=\s+(?:{stop_labels})\b|[.\n]|$)"


def extract_invoice(text: str) -> dict[str, str]:
    return {
        "invoice_number": _first_group(r"(?:invoice\s*(?:number|no|#)?\s*[:\-]?\s*)([A-Z0-9\-/]{3,})", text),
        "invoice_date": _first_group(_date_pattern(r"invoice\s+date|date"), text),
        "total_amount": _first_group(_money_pattern(r"amount\s+due|balance\s+due|grand\s+total|total"), text),
        "currency": _first_group(r"\b(USD|NGN|EUR|GBP|KES|GHS|CAD|AUD)\b", text),
        "vendor_name": _first_group(
            _label_value_pattern(
                r"from|vendor|seller",
                r"invoice|date|amount|balance|grand|total|bill\s+to|ship\s+to|currency",
            ),
            text,
        ),
    }


def extract_receipt(text: str) -> dict[str, str]:
    return {
        "receipt_date": _first_group(_date_pattern(r"date|transaction\s+date"), text),
        "merchant_name": _first_group(_label_value_pattern(r"merchant|store|seller", r"date|subtotal|tax|total|paid"), text),
        "total_amount": _first_group(_money_pattern(r"total|amount\s+paid"), text),
        "tax_amount": _first_group(_money_pattern(r"tax|vat"), text),
        "payment_method": _first_group(r"\b(cash|visa|mastercard|amex|debit|credit|pos|transfer)\b", text),
    }


def extract_contract(text: str) -> dict[str, str]:
    parties = _all_groups(r"(?:between|party)\s+([A-Za-z0-9 .,&'/-]{3,100})", text, limit=4)
    return {
        "effective_date": _first_group(_date_pattern(r"effective\s+date|commencement\s+date"), text),
        "parties": "; ".join(parties),
        "term": _first_group(r"(?:term|duration)\s*[:\-]?\s*([A-Za-z0-9 ,'/-]{3,100}?)(?:\.|\n|$)", text),
        "governing_law": _first_group(r"(?:governing\s+law|laws\s+of)\s*[:\-]?\s*([A-Za-z ,.'-]{3,80})", text),
        "signature_date": _first_group(_date_pattern(r"signature\s+date|signed\s+on"), text),
    }


def extract_purchase_order(text: str) -> dict[str, str]:
    return {
        "purchase_order_number": _first_group(r"(?:purchase\s+order|po)\s*(?:number|no|#)?\s*[:\-]?\s*([A-Z0-9\-/]{3,})", text),
        "order_date": _first_group(_date_pattern(r"order\s+date|date"), text),
        "supplier_name": _first_group(_label_value_pattern(r"supplier|vendor", r"buyer|bill\s+to|date|total|order"), text),
        "buyer_name": _first_group(_label_value_pattern(r"buyer|bill\s+to", r"supplier|vendor|date|total|order"), text),
        "total_amount": _first_group(_money_pattern(r"total|order\s+total"), text),
    }


def extract_bank_statement(text: str) -> dict[str, str]:
    return {
        "account_number": _first_group(r"(?:account\s*(?:number|no)?\s*[:\-]?\s*)([A-Z0-9*.-]{4,})", text),
        "statement_period": _first_group(r"(?:statement\s+period|period)\s*[:\-]?\s*([A-Za-z0-9 ,/\-.]{5,60})", text),
        "opening_balance": _first_group(_money_pattern(r"opening\s+balance"), text),
        "closing_balance": _first_group(_money_pattern(r"closing\s+balance"), text),
        "transactions": "; ".join(_all_groups(r"(\d{1,4}[\-/]\d{1,2}[\-/]\d{1,4}\s+[A-Za-z0-9 .,&'/-]{3,80}\s+[$€£]?\s?[0-9,]+(?:\.[0-9]{2})?)", text, limit=10)),
    }


def extract_generic(text: str) -> dict[str, str]:
    return {
        "document_date": _first_group(_date_pattern(r"date"), text),
        "total_amount": _first_group(_money_pattern(r"total|amount"), text),
        "primary_party": _first_group(_label_value_pattern(r"from|party|vendor|seller", r"to|date|total|amount|customer"), text),
        "secondary_party": _first_group(_label_value_pattern(r"to|bill\s+to|buyer|customer", r"from|date|total|amount|vendor"), text),
        "summary": _normalize_space(text[:300]),
    }


EXTRACTORS = {
    "invoice": extract_invoice,
    "receipt": extract_receipt,
    "contract": extract_contract,
    "purchase_order": extract_purchase_order,
    "bank_statement": extract_bank_statement,
    "generic": extract_generic,
}


def deterministic_extract(text: str, route: str = "invoice") -> dict[str, str]:
    normalized_route = _normalize_route(route)
    return EXTRACTORS[normalized_route](text)


def _field_confidences(fields: dict[str, str], *, ocr_confidence: float, layout_text_used: bool) -> dict[str, float]:
    base = max(0.0, min(1.0, ocr_confidence))
    layout_bonus = 0.05 if layout_text_used else 0.0
    return {
        key: round(min(0.99, base * 0.75 + 0.20 + layout_bonus), 4) if value else 0.0
        for key, value in fields.items()
    }


def confidence_from_fields(ocr_confidence: float, fields: dict[str, str], *, layout_text_used: bool = False) -> float:
    non_empty = sum(1 for value in fields.values() if value)
    fill_ratio = non_empty / max(len(fields), 1)
    layout_bonus = 0.05 if layout_text_used else 0.0
    return round(min(0.99, (0.60 * ocr_confidence) + (0.35 * fill_ratio) + layout_bonus), 4)


def _extract_json_blob(content: str) -> dict[str, Any]:
    content = content.strip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _schema_for_route(route: str) -> list[str]:
    return SCHEMAS[_normalize_route(route)]


async def vlm_fallback_extract(text: str, route: str, *, strategy_profile: str | None = None) -> dict[str, Any]:
    from langchain_openai import ChatOpenAI

    schema = _schema_for_route(route)
    llm = ChatOpenAI(
        model=settings.vlm_model,
        api_key=settings.vlm_api_key,
        base_url=f"{settings.vlm_base_url}/v1",
        temperature=0,
        timeout=int(getattr(settings, "extraction_vlm_timeout_seconds", 60)),
    )

    prompt = (
        "Extract structured fields from this document text. Return strict JSON only. "
        f"Document route: {route}. Strategy profile: {strategy_profile or 'default'}. "
        f"Required keys: {', '.join(schema)}, confidence. "
        "Use empty strings for missing fields and a numeric confidence from 0 to 1.\n\n"
        f"TEXT:\n{text[: int(getattr(settings, 'extraction_prompt_max_chars', 8000))]}"
    )
    response = await llm.ainvoke(prompt)
    return _extract_json_blob(response.content if isinstance(response.content, str) else str(response.content))


def _build_context_sync(request: ExtractRequest) -> dict[str, Any]:
    ocr = download_json(settings, settings.ocr_bucket, request.ocr_key)
    layout = download_json(settings, settings.layout_bucket, request.layout_key)
    ocr_text, detected_ocr_confidence, ocr_fallback_used = _extract_text_from_ocr(ocr)
    layout_text, layout_blocks = _extract_text_from_layout(layout)
    layout_text_used = bool(layout_text and len(layout_text) >= max(20, len(ocr_text) // 2))
    text = layout_text if layout_text_used else ocr_text
    return {
        "ocr": ocr,
        "layout": layout,
        "text": text,
        "ocr_text": ocr_text,
        "layout_text": layout_text,
        "layout_blocks": layout_blocks,
        "layout_text_used": layout_text_used,
        "ocr_confidence": request.ocr_confidence if request.ocr_confidence > 0 else detected_ocr_confidence,
        "ocr_fallback_used": ocr_fallback_used,
    }


def _merge_llm_fields(fields: dict[str, str], 
                      llm_fields: dict[str, Any], 
                      schema: list[str]) -> tuple[dict[str, str], list[str]]:
    merged = dict(fields)
    updated: list[str] = []
    for key in schema:
        value = llm_fields.get(key)
        if value is not None and str(value).strip() and not merged.get(key):
            merged[key] = _normalize_space(str(value))
            updated.append(key)
    return merged, updated


def _persist_payload(request: ExtractRequest, 
                     payload: dict[str, Any]) -> tuple[str, str]:
    key = f"jobs/{request.job_id}/extraction/result.json"
    extraction_bucket = settings.extraction_bucket
    try:
        upload_json(settings, extraction_bucket, key, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "primary extraction artifact upload failed job_id=%s bucket=%s",
            request.job_id,
            extraction_bucket,
            exc_info=exc,
        )
        fallback_bucket = settings.layout_bucket
        if fallback_bucket == extraction_bucket:
            raise
        upload_json(settings, fallback_bucket, key, payload)
        extraction_bucket = fallback_bucket
        logger.warning("extraction artifact upload fell back job_id=%s fallback_bucket=%s", request.job_id, fallback_bucket)
    return extraction_bucket, key


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/extract")
async def extract(request: ExtractRequest) -> dict[str, Any]:
    if not _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="extraction worker is busy")

    started = time.perf_counter()
    future: Future[dict[str, Any]] = _EXTRACTION_POOL.submit(_build_context_sync, request)
    future.add_done_callback(_release_request_slot)
    timeout = max(1, int(getattr(settings, "extraction_request_timeout_seconds", 90)))
    try:
        context = await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="extraction request timed out") from exc

    route = _normalize_route(request.route)
    schema = _schema_for_route(route)
    fields = deterministic_extract(context["text"], route)
    ocr_confidence = float(context["ocr_confidence"])
    confidence = confidence_from_fields(ocr_confidence, fields, layout_text_used=bool(context["layout_text_used"]))

    used_vlm_fallback = False
    fallback_reason = ""
    llm_updated_fields: list[str] = []
    should_use_vlm = (
        confidence < settings.vlm_fallback_threshold
        or route == "generic"
        or bool(context["ocr_fallback_used"])
    )
    if should_use_vlm and bool(getattr(settings, "extraction_enable_vlm_fallback", True)):
        try:
            llm_fields = await vlm_fallback_extract(
                context["text"],
                route,
                strategy_profile=request.strategy_profile,
            )
            if llm_fields:
                fields, llm_updated_fields = _merge_llm_fields(fields, llm_fields, schema)
                model_conf = llm_fields.get("confidence")
                if isinstance(model_conf, (int, float)):
                    confidence = round(max(confidence, min(1.0, float(model_conf))), 4)
            used_vlm_fallback = True
            fallback_reason = "confidence_or_generic_route"
        except Exception as exc:  # noqa: BLE001
            logger.exception("VLM fallback extraction failed job_id=%s", request.job_id, exc_info=exc)
            used_vlm_fallback = False
            fallback_reason = "vlm_failed"

    field_confidences = _field_confidences(fields, ocr_confidence=ocr_confidence, layout_text_used=bool(context["layout_text_used"]))
    missing_fields = [key for key in schema if not fields.get(key)]
    if missing_fields:
        confidence = round(min(confidence, confidence_from_fields(ocr_confidence, fields, layout_text_used=bool(context["layout_text_used"]))), 4)

    payload = {
        "job_id": request.job_id,
        "route": route,
        "strategy_profile": request.strategy_profile,
        "extraction_mode": request.extraction_mode,
        "confidence": confidence,
        "used_vlm_fallback": used_vlm_fallback,
        "fallback_reason": fallback_reason,
        "fields": fields,
        "field_confidences": field_confidences,
        "missing_fields": missing_fields,
        "llm_updated_fields": llm_updated_fields,
        "source": {
            "ocr_key": request.ocr_key,
            "layout_key": request.layout_key,
            "layout_text_used": bool(context["layout_text_used"]),
            "ocr_fallback_used": bool(context["ocr_fallback_used"]),
        },
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }

    extraction_bucket, key = await asyncio.to_thread(_persist_payload, request, payload)
    return {
        "job_id": request.job_id,
        "extraction_bucket": extraction_bucket,
        "extraction_key": key,
        "fields": fields,
        "confidence": confidence,
        "used_vlm_fallback": used_vlm_fallback,
    }
