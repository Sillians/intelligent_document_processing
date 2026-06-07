from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
_ROUTER_MAX_INFLIGHT = max(1, int(getattr(settings, "classifier_max_inflight_requests", 8)))
_ROUTER_POOL = ThreadPoolExecutor(max_workers=_ROUTER_MAX_INFLIGHT, thread_name_prefix="classifier-router")
_ROUTER_INFLIGHT = 0
_ROUTER_INFLIGHT_LOCK = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _ROUTER_POOL.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="classifier-router-service", lifespan=lifespan)
instrument_app(app, "classifier-router-service")


class RouteRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    ocr_key: str = Field(min_length=1, max_length=1024)
    layout_key: str | None = Field(default=None, max_length=1024)


@dataclass(frozen=True)
class RouteRule:
    route: str
    strategy_profile: str
    extraction_mode: str
    threshold: float
    patterns: tuple[tuple[str, float], ...]
    expected_fields: tuple[str, ...]


ROUTE_RULES: tuple[RouteRule, ...] = (
    RouteRule(
        route="invoice",
        strategy_profile="invoice_v1",
        extraction_mode="deterministic_then_vlm",
        threshold=0.62,
        patterns=(
            (r"\binvoice\b", 0.28),
            (r"\binvoice\s*(?:no|number|#)\b", 0.20),
            (r"\bbill\s+to\b", 0.16),
            (r"\bship\s+to\b", 0.10),
            (r"\bamount\s+due\b", 0.18),
            (r"\btotal\b", 0.08),
            (r"\bpayment\s+terms\b", 0.10),
        ),
        expected_fields=("invoice_number", "invoice_date", "total_amount", "currency", "vendor_name"),
    ),
    RouteRule(
        route="receipt",
        strategy_profile="receipt_v1",
        extraction_mode="deterministic_then_vlm",
        threshold=0.58,
        patterns=(
            (r"\breceipt\b", 0.26),
            (r"\bsubtotal\b", 0.16),
            (r"\btax\b", 0.10),
            (r"\bchange\b", 0.12),
            (r"\bmerchant\b", 0.10),
            (r"\bpaid\b", 0.10),
        ),
        expected_fields=("receipt_date", "merchant_name", "total_amount", "tax_amount", "payment_method"),
    ),
    RouteRule(
        route="contract",
        strategy_profile="contract_v1",
        extraction_mode="layout_aware_vlm",
        threshold=0.58,
        patterns=(
            (r"\bagreement\b", 0.24),
            (r"\bcontract\b", 0.24),
            (r"\bterms\b", 0.12),
            (r"\bparty\b", 0.12),
            (r"\bwhereas\b", 0.18),
            (r"\bsignature\b", 0.10),
        ),
        expected_fields=("effective_date", "parties", "term", "governing_law", "signature_date"),
    ),
    RouteRule(
        route="purchase_order",
        strategy_profile="purchase_order_v1",
        extraction_mode="deterministic_then_vlm",
        threshold=0.58,
        patterns=(
            (r"\bpurchase\s+order\b", 0.30),
            (r"\bpo\s*(?:no|number|#)\b", 0.20),
            (r"\bbuyer\b", 0.12),
            (r"\bsupplier\b", 0.12),
            (r"\bline\s+items?\b", 0.12),
        ),
        expected_fields=("purchase_order_number", "order_date", "supplier_name", "buyer_name", "total_amount"),
    ),
    RouteRule(
        route="bank_statement",
        strategy_profile="bank_statement_v1",
        extraction_mode="table_aware_vlm",
        threshold=0.60,
        patterns=(
            (r"\bbank\s+statement\b", 0.30),
            (r"\baccount\s+(?:number|no)\b", 0.18),
            (r"\bopening\s+balance\b", 0.16),
            (r"\bclosing\s+balance\b", 0.16),
            (r"\btransaction\b", 0.12),
        ),
        expected_fields=("account_number", "statement_period", "opening_balance", "closing_balance", "transactions"),
    ),
)


GENERIC_PROFILE = {
    "route": "generic",
    "strategy_profile": "generic_v1",
    "extraction_mode": "layout_aware_vlm",
    "expected_fields": [],
}


def _try_acquire_request_slot() -> bool:
    global _ROUTER_INFLIGHT
    with _ROUTER_INFLIGHT_LOCK:
        if _ROUTER_INFLIGHT >= _ROUTER_MAX_INFLIGHT:
            return False
        _ROUTER_INFLIGHT += 1
        return True


def _release_request_slot(_: Future[Any] | None = None) -> None:
    global _ROUTER_INFLIGHT
    with _ROUTER_INFLIGHT_LOCK:
        _ROUTER_INFLIGHT = max(0, _ROUTER_INFLIGHT - 1)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _extract_ocr_text(ocr_data: dict[str, Any]) -> tuple[str, int, float, bool]:
    tokens = [token for token in ocr_data.get("tokens", []) if isinstance(token, dict)]
    token_texts = [str(token.get("text") or "").strip() for token in tokens if str(token.get("text") or "").strip()]
    confidences = [
        float(token.get("confidence"))
        for token in tokens
        if isinstance(token.get("confidence"), (int, float))
    ]
    text = " ".join(token_texts).strip() or str(ocr_data.get("full_text") or "").strip()
    fallback_used = bool(ocr_data.get("fallback_used")) or "ocr_fallback:" in text.lower()
    mean_confidence = round(mean(confidences), 4) if confidences else float(ocr_data.get("mean_confidence") or 0.0)
    return text, len(token_texts), mean_confidence, fallback_used


def _load_layout_features(layout_key: str | None) -> dict[str, Any]:
    if not layout_key:
        return {}
    try:
        layout = download_json(settings, settings.layout_bucket, layout_key)
    except Exception:  # noqa: BLE001
        return {}

    blocks = layout.get("blocks", []) if isinstance(layout, dict) else []
    block_types = [str(block.get("type") or "unknown") for block in blocks if isinstance(block, dict)]
    return {
        "layout_key": layout_key,
        "block_count": int(layout.get("block_count") or len(blocks)) if isinstance(layout, dict) else len(blocks),
        "text_zone_count": int(layout.get("text_zone_count") or 0) if isinstance(layout, dict) else 0,
        "block_types": sorted(set(block_types)),
    }


def _pattern_evidence(pattern: str, normalized_text: str) -> list[str]:
    evidence: list[str] = []
    for match in re.finditer(pattern, normalized_text, flags=re.IGNORECASE):
        start = max(0, match.start() - 40)
        end = min(len(normalized_text), match.end() + 40)
        snippet = normalized_text[start:end].strip()
        if snippet and snippet not in evidence:
            evidence.append(snippet)
        if len(evidence) >= 3:
            break
    return evidence


def _score_rule(rule: RouteRule, normalized_text: str) -> tuple[float, list[dict[str, Any]]]:
    matched: list[dict[str, Any]] = []
    score = 0.0
    for pattern, weight in rule.patterns:
        if re.search(pattern, normalized_text, flags=re.IGNORECASE):
            score += weight
            matched.append(
                {
                    "pattern": pattern,
                    "weight": weight,
                    "evidence": _pattern_evidence(pattern, normalized_text),
                }
            )
    return min(score, 0.85), matched


def classify_document(
    text: str,
    *,
    token_count: int,
    mean_ocr_confidence: float,
    fallback_used: bool,
    layout_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_text(text)
    min_text_chars = int(getattr(settings, "classifier_min_text_chars", 12))
    auto_threshold = float(getattr(settings, "classifier_auto_route_threshold", 0.68))

    if len(normalized) < min_text_chars or fallback_used:
        confidence = 0.35 if fallback_used else 0.45
        return {
            **GENERIC_PROFILE,
            "classification_confidence": confidence,
            "confidence_band": "low",
            "auto_route": False,
            "requires_review": True,
            "reason": "ocr_fallback" if fallback_used else "insufficient_text",
            "matched_signals": [],
            "layout_features": layout_features or {},
        }

    candidates: list[dict[str, Any]] = []
    for rule in ROUTE_RULES:
        rule_score, matched = _score_rule(rule, normalized)
        confidence = min(0.99, 0.20 + rule_score + min(mean_ocr_confidence, 1.0) * 0.12)
        candidates.append(
            {
                "route": rule.route,
                "strategy_profile": rule.strategy_profile,
                "extraction_mode": rule.extraction_mode,
                "expected_fields": list(rule.expected_fields),
                "classification_confidence": round(confidence, 4),
                "matched_signals": matched,
                "matched_signal_count": len(matched),
                "threshold": rule.threshold,
            }
        )

    best = max(candidates, key=lambda item: (item["classification_confidence"], item["matched_signal_count"]))
    if best["classification_confidence"] < best["threshold"]:
        best = {
            **GENERIC_PROFILE,
            "classification_confidence": 0.50,
            "matched_signals": [],
            "matched_signal_count": 0,
            "threshold": auto_threshold,
        }

    confidence = float(best["classification_confidence"])
    confidence_band = "high" if confidence >= 0.85 else "medium" if confidence >= auto_threshold else "low"
    auto_route = confidence >= auto_threshold and best["route"] != "generic"
    return {
        **best,
        "classification_confidence": round(confidence, 4),
        "confidence_band": confidence_band,
        "auto_route": auto_route,
        "requires_review": not auto_route,
        "reason": "matched_rules" if best["route"] != "generic" else "no_route_threshold_met",
        "layout_features": layout_features or {},
    }


def _route_sync(request: RouteRequest) -> dict[str, Any]:
    started = time.perf_counter()
    ocr = download_json(settings, settings.ocr_bucket, request.ocr_key)
    text, token_count, mean_ocr_confidence, fallback_used = _extract_ocr_text(ocr)
    layout_features = _load_layout_features(request.layout_key)
    decision = classify_document(
        text,
        token_count=token_count,
        mean_ocr_confidence=mean_ocr_confidence,
        fallback_used=fallback_used,
        layout_features=layout_features,
    )

    payload = {
        "job_id": request.job_id,
        "ocr_key": request.ocr_key,
        "layout_key": request.layout_key,
        "route": decision["route"],
        "strategy_profile": decision["strategy_profile"],
        "extraction_mode": decision["extraction_mode"],
        "expected_fields": decision["expected_fields"],
        "classification_confidence": decision["classification_confidence"],
        "confidence_band": decision["confidence_band"],
        "auto_route": decision["auto_route"],
        "requires_review": decision["requires_review"],
        "reason": decision["reason"],
        "matched_signals": decision["matched_signals"],
        "layout_features": decision["layout_features"],
        "ocr_summary": {
            "token_count": token_count,
            "mean_confidence": mean_ocr_confidence,
            "fallback_used": fallback_used,
            "text_chars": len(text),
        },
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }

    classification_key = ""
    if bool(getattr(settings, "classifier_persist_decision", True)):
        classification_key = f"jobs/{request.job_id}/classification/route.json"
        upload_json(settings, settings.layout_bucket, classification_key, payload)

    return {
        "job_id": request.job_id,
        "route": payload["route"],
        "strategy_profile": payload["strategy_profile"],
        "extraction_mode": payload["extraction_mode"],
        "classification_confidence": payload["classification_confidence"],
        "confidence_band": payload["confidence_band"],
        "auto_route": payload["auto_route"],
        "requires_review": payload["requires_review"],
        "classification_bucket": settings.layout_bucket if classification_key else "",
        "classification_key": classification_key,
        "matched_signal_count": len(payload["matched_signals"]),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/route")
async def route_document(request: RouteRequest) -> dict[str, Any]:
    if not _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="classifier router is busy")

    future = _ROUTER_POOL.submit(_route_sync, request)
    future.add_done_callback(_release_request_slot)
    timeout = max(1, int(getattr(settings, "classifier_request_timeout_seconds", 15)))
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="classifier router request timed out") from exc
