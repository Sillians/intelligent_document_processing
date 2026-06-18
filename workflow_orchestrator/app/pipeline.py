from __future__ import annotations

from typing import Any


class PipelineContractError(ValueError):
    """Raised when an activity response violates the expected contract."""


def required_str(payload: dict[str, Any], key: str, *, stage: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PipelineContractError(f"{stage}: expected non-empty string field '{key}'")
    return value.strip()


def required_dict(payload: dict[str, Any], key: str, *, stage: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise PipelineContractError(f"{stage}: expected object field '{key}'")
    return value


def required_float(payload: dict[str, Any], key: str, *, stage: str) -> float:
    value = payload.get(key)
    if value is None:
        raise PipelineContractError(f"{stage}: missing numeric field '{key}'")

    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PipelineContractError(f"{stage}: invalid numeric field '{key}'") from exc


def required_bool(payload: dict[str, Any], key: str, *, stage: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False

    raise PipelineContractError(f"{stage}: invalid boolean field '{key}'")


def optional_list_of_str(payload: dict[str, Any], key: str, *, stage: str) -> list[str]:
    value = payload.get(key)
    if value is None:
        return []

    if not isinstance(value, list):
        raise PipelineContractError(f"{stage}: expected list field '{key}'")

    output: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise PipelineContractError(f"{stage}: expected '{key}[{idx}]' to be string")
        output.append(item)
    return output


def build_preprocess_payload(start_payload: dict[str, Any]) -> dict[str, str]:
    job_id = required_str(start_payload, "job_id", stage="workflow_input")
    raw_bucket = required_str(start_payload, "raw_bucket", stage="workflow_input")
    raw_key = required_str(start_payload, "raw_key", stage="workflow_input")

    return {
        "job_id": job_id,
        "raw_bucket": raw_bucket,
        "raw_key": raw_key,
    }


def build_ocr_payload(job_id: str, preprocess_result: dict[str, Any]) -> dict[str, str]:
    preprocessed_key = required_str(preprocess_result, "preprocessed_key", stage="preprocess")
    return {"job_id": job_id, "preprocessed_key": preprocessed_key}


def build_layout_payload(job_id: str, preprocess_result: dict[str, Any], ocr_result: dict[str, Any]) -> dict[str, str]:
    preprocessed_key = required_str(preprocess_result, "preprocessed_key", stage="preprocess")
    ocr_key = required_str(ocr_result, "ocr_key", stage="ocr")
    return {
        "job_id": job_id,
        "preprocessed_key": preprocessed_key,
        "ocr_key": ocr_key,
    }


def build_classification_payload(job_id: str, ocr_result: dict[str, Any]) -> dict[str, str]:
    ocr_key = required_str(ocr_result, "ocr_key", stage="ocr")
    return {"job_id": job_id, "ocr_key": ocr_key}


def build_extraction_payload(
    job_id: str,
    ocr_result: dict[str, Any],
    layout_result: dict[str, Any],
    classification_result: dict[str, Any],
) -> dict[str, Any]:
    ocr_key = required_str(ocr_result, "ocr_key", stage="ocr")
    layout_key = required_str(layout_result, "layout_key", stage="layout")
    route = required_str(classification_result, "route", stage="classification")
    ocr_confidence = required_float(ocr_result, "mean_confidence", stage="ocr")

    return {
        "job_id": job_id,
        "ocr_key": ocr_key,
        "layout_key": layout_key,
        "ocr_confidence": ocr_confidence,
        "route": route,
        "strategy_profile": classification_result.get("strategy_profile"),
        "extraction_mode": classification_result.get("extraction_mode"),
    }


def build_validation_payload(job_id: str, extraction_result: dict[str, Any]) -> dict[str, Any]:
    extraction_key = required_str(extraction_result, "extraction_key", stage="extraction")
    fields = required_dict(extraction_result, "fields", stage="extraction")
    confidence = required_float(extraction_result, "confidence", stage="extraction")
    used_vlm_fallback = required_bool(extraction_result, "used_vlm_fallback", stage="extraction")

    payload: dict[str, Any] = {
        "job_id": job_id,
        "extraction_key": extraction_key,
        "fields": fields,
        "confidence": confidence,
        "used_vlm_fallback": used_vlm_fallback,
    }
    extraction_bucket = extraction_result.get("extraction_bucket")
    if isinstance(extraction_bucket, str) and extraction_bucket.strip():
        payload["extraction_bucket"] = extraction_bucket.strip()
    route = extraction_result.get("route")
    if isinstance(route, str) and route.strip():
        payload["route"] = route.strip()
    return payload


def should_route_to_human_review(validation_result: dict[str, Any]) -> bool:
    return required_bool(validation_result, "requires_human_review", stage="validation")


def build_review_payload(job_id: str, extraction_result: dict[str, Any], validation_result: dict[str, Any]) -> dict[str, Any]:
    reasons = optional_list_of_str(validation_result, "reasons", stage="validation")
    fields = required_dict(extraction_result, "fields", stage="extraction")
    confidence = required_float(extraction_result, "confidence", stage="extraction")

    payload: dict[str, Any] = {
        "job_id": job_id,
        "reasons": reasons,
        "fields": fields,
        "confidence": confidence,
    }
    for source, keys in (
        (extraction_result, ("extraction_bucket", "extraction_key", "route")),
        (validation_result, ("validation_bucket", "validation_key", "verdict")),
    ):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                payload[key] = value.strip()
    return payload


def build_delivery_payload(
    job_id: str,
    extraction_result: dict[str, Any],
    validation_result: dict[str, Any],
) -> dict[str, Any]:
    fields = required_dict(extraction_result, "fields", stage="extraction")
    verdict = required_str(validation_result, "verdict", stage="validation")
    payload: dict[str, Any] = {
        "job_id": job_id,
        "payload": fields,
        "approval_status": verdict,
    }
    for source, keys in (
        (extraction_result, ("extraction_bucket", "extraction_key")),
        (validation_result, ("validation_bucket", "validation_key")),
    ):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                payload[key] = value.strip()
    route = extraction_result.get("route")
    if isinstance(route, str) and route.strip():
        payload["metadata"] = {"route": route.strip()}
    return payload


def build_evaluation_payload(
    *,
    job_id: str,
    final_status: str,
    ocr_result: dict[str, Any] | None,
    extraction_result: dict[str, Any] | None,
    validation_result: dict[str, Any] | None,
) -> dict[str, Any]:
    ocr_confidence = 0.0
    extraction_confidence = 0.0
    used_vlm_fallback = False
    requires_human_review = False

    if ocr_result:
        try:
            ocr_confidence = required_float(ocr_result, "mean_confidence", stage="ocr")
        except PipelineContractError:
            ocr_confidence = 0.0

    if extraction_result:
        try:
            extraction_confidence = required_float(extraction_result, "confidence", stage="extraction")
        except PipelineContractError:
            extraction_confidence = 0.0

        try:
            used_vlm_fallback = required_bool(extraction_result, "used_vlm_fallback", stage="extraction")
        except PipelineContractError:
            used_vlm_fallback = False

    if validation_result:
        try:
            requires_human_review = required_bool(
                validation_result,
                "requires_human_review",
                stage="validation",
            )
        except PipelineContractError:
            requires_human_review = False

    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": final_status,
        "ocr_confidence": ocr_confidence,
        "extraction_confidence": extraction_confidence,
        "used_vlm_fallback": used_vlm_fallback,
        "requires_human_review": requires_human_review,
    }
    if extraction_result:
        fields = extraction_result.get("fields")
        if isinstance(fields, dict):
            payload["field_count"] = len(fields)
            payload["populated_field_count"] = sum(
                1 for value in fields.values() if value is not None and str(value).strip()
            )
    if validation_result:
        route = validation_result.get("route")
        if isinstance(route, str) and route.strip():
            payload["route"] = route.strip()
        verdict = validation_result.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            payload["validation_verdict"] = verdict.strip()
    return payload


def build_webhook_event_payload(
    *,
    event_type: str,
    tenant_id: str,
    job_id: str,
    workflow_id: str | None,
    final_status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    normalized_tenant_id = tenant_id or "default"
    data: dict[str, Any] = {
        "status": final_status,
    }
    if result:
        data.update(
            {
                "classification": result.get("classification"),
                "validation": result.get("validation"),
                "delivery": result.get("delivery"),
                "review_task": result.get("review_task"),
            }
        )
    if error:
        data["error"] = error[:500]

    return {
        "event_type": event_type,
        "tenant_id": normalized_tenant_id,
        "job_id": job_id,
        "workflow_id": workflow_id,
        "data": data,
        "idempotency_key": f"{event_type}:{normalized_tenant_id}:{job_id}:{workflow_id or ''}",
    }
