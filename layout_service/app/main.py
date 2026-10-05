from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
import logging
import math
import time
from statistics import mean
from threading import Lock
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_bytes, download_json, upload_json
from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope
from shared.idp_common.internal_auth import install_internal_auth
from shared.idp_common.page_contract import SCHEMA_VERSION, validate_pages

settings = get_settings()
logger = logging.getLogger("layout_service")

_LAYOUT_MODEL = None
_LAYOUT_ERROR: str | None = None
_LAYOUT_MODEL_LOCK = Lock()
_LAYOUT_MAX_INFLIGHT = max(1, int(getattr(settings, "layout_max_inflight_requests", 2)))
_LAYOUT_POOL = ThreadPoolExecutor(max_workers=_LAYOUT_MAX_INFLIGHT, thread_name_prefix="layout-worker")
_LAYOUT_INFLIGHT = 0
_LAYOUT_INFLIGHT_LOCK = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _LAYOUT_POOL.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="layout-service", lifespan=lifespan)
instrument_app(app, "layout-service")
install_internal_auth(app)


class LayoutRequest(BaseModel):
    page_slot: int | None = Field(default=None, ge=1, le=1000)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    preprocessed_key: str = Field(min_length=1, max_length=1024)
    ocr_key: str = Field(min_length=1, max_length=1024)
    preprocessed_pages: list[dict[str, Any]] | None = None


def _try_acquire_request_slot() -> bool:
    global _LAYOUT_INFLIGHT
    with _LAYOUT_INFLIGHT_LOCK:
        if _LAYOUT_INFLIGHT >= _LAYOUT_MAX_INFLIGHT:
            return False
        _LAYOUT_INFLIGHT += 1
        return True


def _release_request_slot(_: Future[Any] | None = None) -> None:
    global _LAYOUT_INFLIGHT
    with _LAYOUT_INFLIGHT_LOCK:
        _LAYOUT_INFLIGHT = max(0, _LAYOUT_INFLIGHT - 1)


def get_layout_model():
    global _LAYOUT_MODEL, _LAYOUT_ERROR
    if _LAYOUT_MODEL is not None or _LAYOUT_ERROR is not None:
        return _LAYOUT_MODEL

    with _LAYOUT_MODEL_LOCK:
        if _LAYOUT_MODEL is not None or _LAYOUT_ERROR is not None:
            return _LAYOUT_MODEL

        try:
            import layoutparser as lp

            _LAYOUT_MODEL = lp.Detectron2LayoutModel(
                settings.layout_model_config,
                extra_config=[
                    "MODEL.ROI_HEADS.SCORE_THRESH_TEST",
                    float(getattr(settings, "layout_model_score_threshold", 0.5)),
                ],
                label_map={0: "text", 1: "title", 2: "list", 3: "table", 4: "figure"},
            )
        except Exception as exc:  # noqa: BLE001
            _LAYOUT_ERROR = str(exc)[:500]
            _LAYOUT_MODEL = None
            logger.warning("Layout model unavailable; using heuristic backend: %s", _LAYOUT_ERROR)

    return _LAYOUT_MODEL


def _decode_image(preprocessed_key: str) -> np.ndarray | None:
    raw = download_bytes(settings, settings.preprocessed_bucket, preprocessed_key)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _flatten_points(value: Any) -> list[tuple[float, float]]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()

    if isinstance(value, dict):
        keys = {"x1", "y1", "x2", "y2"}
        if keys.issubset(value):
            return [
                (_to_float(value["x1"]), _to_float(value["y1"])),
                (_to_float(value["x2"]), _to_float(value["y2"])),
            ]
        return []

    if isinstance(value, (list, tuple)):
        if len(value) >= 4 and all(isinstance(v, (int, float)) for v in value[:4]):
            return [(_to_float(value[0]), _to_float(value[1])), (_to_float(value[2]), _to_float(value[3]))]

        points: list[tuple[float, float]] = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                if isinstance(item[0], (list, tuple)):
                    points.extend(_flatten_points(item))
                else:
                    points.append((_to_float(item[0]), _to_float(item[1])))
        return points

    return []


def _bbox_from_value(value: Any) -> tuple[float, float, float, float] | None:
    points = _flatten_points(value)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    if math.isclose(x1, x2) or math.isclose(y1, y2):
        return None
    return x1, y1, x2, y2


def _clamp_box(
    box: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return (
        max(0, min(width, int(round(x1)))),
        max(0, min(height, int(round(y1)))),
        max(0, min(width, int(round(x2)))),
        max(0, min(height, int(round(y2)))),
    )


def _box_area(box: dict[str, Any]) -> float:
    return max(0.0, float(box["x2"] - box["x1"])) * max(0.0, float(box["y2"] - box["y1"]))


def _intersection_area(a: dict[str, Any], b: dict[str, Any]) -> float:
    x1 = max(float(a["x1"]), float(b["x1"]))
    y1 = max(float(a["y1"]), float(b["y1"]))
    x2 = min(float(a["x2"]), float(b["x2"]))
    y2 = min(float(a["y2"]), float(b["y2"]))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _normalize_ocr_tokens(ocr_data: dict[str, Any], *, width: int, height: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, token in enumerate(ocr_data.get("tokens", [])):
        if not isinstance(token, dict):
            continue
        text = str(token.get("text") or "").strip()
        if not text:
            continue

        raw_bbox = token.get("bbox") or token.get("box") or token.get("polygon")
        bbox = _bbox_from_value(raw_bbox)
        if bbox is None:
            continue
        x1, y1, x2, y2 = _clamp_box(bbox, width=width, height=height)
        if x2 <= x1 or y2 <= y1:
            continue

        normalized.append(
            {
                "index": idx,
                "text": text,
                "confidence": _to_float(token.get("confidence"), 0.0),
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "cx": round((x1 + x2) / 2, 3),
                "cy": round((y1 + y2) / 2, 3),
            }
        )
    return normalized


def _block_from_detected(item: Any, *, block_id: str, width: int, height: int) -> dict[str, Any] | None:
    block = getattr(item, "block", item)
    box = (
        _to_float(getattr(block, "x_1", 0)),
        _to_float(getattr(block, "y_1", 0)),
        _to_float(getattr(block, "x_2", 0)),
        _to_float(getattr(block, "y_2", 0)),
    )
    x1, y1, x2, y2 = _clamp_box(box, width=width, height=height)
    if x2 <= x1 or y2 <= y1:
        return None

    score = _to_float(getattr(item, "score", 0.0), 0.0)
    if score < float(getattr(settings, "layout_min_block_score", 0.35)):
        return None

    return {
        "id": block_id,
        "type": str(getattr(item, "type", "text") or "text"),
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "score": round(score, 4),
        "source": "detectron2",
    }


def _detect_layout_blocks(image: np.ndarray) -> tuple[list[dict[str, Any]], str]:
    height, width = image.shape[:2]
    model = get_layout_model()
    if model is None:
        return [], "heuristic"

    detected = model.detect(image)
    blocks: list[dict[str, Any]] = []
    for idx, item in enumerate(detected):
        block = _block_from_detected(item, block_id=f"b{idx:04d}", width=width, height=height)
        if block is not None:
            blocks.append(block)
    return blocks, "detectron2"


def _heuristic_blocks_from_tokens(tokens: list[dict[str, Any]], *, width: int, height: int) -> list[dict[str, Any]]:
    if not tokens:
        return [
            {
                "id": "b0000",
                "type": "page",
                "x1": 0,
                "y1": 0,
                "x2": width,
                "y2": height,
                "score": 0.5,
                "source": "heuristic_page",
            }
        ]

    tolerance = int(getattr(settings, "layout_line_group_y_tolerance", 18))
    sorted_tokens = sorted(tokens, key=lambda t: (t["cy"], t["x1"]))
    lines: list[list[dict[str, Any]]] = []
    for token in sorted_tokens:
        if not lines or abs(mean(t["cy"] for t in lines[-1]) - token["cy"]) > tolerance:
            lines.append([token])
        else:
            lines[-1].append(token)

    blocks: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        x1 = max(0, min(t["x1"] for t in line) - 4)
        y1 = max(0, min(t["y1"] for t in line) - 4)
        x2 = min(width, max(t["x2"] for t in line) + 4)
        y2 = min(height, max(t["y2"] for t in line) + 4)
        blocks.append(
            {
                "id": f"b{idx:04d}",
                "type": "text",
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "score": 0.65,
                "source": "heuristic_ocr_line",
            }
        )
    return blocks


def _assign_tokens_to_blocks(blocks: list[dict[str, Any]], tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    min_overlap = float(getattr(settings, "layout_min_token_overlap", 0.1))
    enriched: list[dict[str, Any]] = []
    assigned_token_indexes: set[int] = set()

    for order, block in enumerate(sorted(blocks, key=lambda b: (b["y1"], b["x1"]))):
        block_tokens: list[dict[str, Any]] = []
        for token in tokens:
            token_area = _box_area(token)
            overlap_ratio = _intersection_area(block, token) / token_area if token_area > 0 else 0.0
            centroid_inside = block["x1"] <= token["cx"] <= block["x2"] and block["y1"] <= token["cy"] <= block["y2"]
            if overlap_ratio >= min_overlap or centroid_inside:
                block_tokens.append(token)
                assigned_token_indexes.add(int(token["index"]))

        block_tokens.sort(key=lambda t: (t["y1"], t["x1"]))
        confidences = [float(t["confidence"]) for t in block_tokens]
        enriched_block = {
            **block,
            "reading_order": order,
            "token_count": len(block_tokens),
            "token_indices": [int(t["index"]) for t in block_tokens],
            "text": " ".join(t["text"] for t in block_tokens).strip(),
            "mean_confidence": round(mean(confidences), 4) if confidences else 0.0,
        }
        enriched.append(enriched_block)

    if tokens and len(assigned_token_indexes) < len(tokens):
        remainder = [t for t in tokens if int(t["index"]) not in assigned_token_indexes]
        remainder.sort(key=lambda t: (t["y1"], t["x1"]))
        x1 = min(t["x1"] for t in remainder)
        y1 = min(t["y1"] for t in remainder)
        x2 = max(t["x2"] for t in remainder)
        y2 = max(t["y2"] for t in remainder)
        confidences = [float(t["confidence"]) for t in remainder]
        enriched.append(
            {
                "id": f"b{len(enriched):04d}",
                "type": "text",
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "score": 0.45,
                "source": "heuristic_unassigned_tokens",
                "reading_order": len(enriched),
                "token_count": len(remainder),
                "token_indices": [int(t["index"]) for t in remainder],
                "text": " ".join(t["text"] for t in remainder).strip(),
                "mean_confidence": round(mean(confidences), 4) if confidences else 0.0,
            }
        )

    return enriched


def _analyze_layout_sync(request: LayoutRequest) -> dict[str, Any]:
    require_artifact_scope(request.tenant_id, request.job_id, request.preprocessed_key)
    require_artifact_scope(request.tenant_id, request.job_id, request.ocr_key)
    started = time.perf_counter()
    ocr_data = download_json(settings, settings.ocr_bucket, request.ocr_key)
    if request.page_slot is not None and (ocr_data.get("page_slot") != request.page_slot or ocr_data.get("tenant_id") != request.tenant_id or ocr_data.get("job_id") != request.job_id):
        raise HTTPException(422, "Independent OCR artifact identity does not match layout request")
    source_pages = validate_pages(
        request.preprocessed_pages or [{"page_number": 1, "preprocessed_key": request.preprocessed_key}],
        tenant_id=request.tenant_id, job_id=request.job_id, key_field="preprocessed_key", first_key=request.preprocessed_key,
    )
    ocr_pages = ocr_data.get("pages") or [{"page_number": 1, "tokens": ocr_data.get("tokens", [])}]
    if len(ocr_pages) != len(source_pages):
        raise HTTPException(status_code=422, detail="OCR page count does not match preprocessing")
    pages: list[dict[str, Any]] = []
    enriched_blocks: list[dict[str, Any]] = []
    for source_page, ocr_page in zip(source_pages, ocr_pages):
        number = source_page["page_number"]
        if ocr_page.get("page_number") != number:
            raise HTTPException(status_code=422, detail="OCR page order does not match preprocessing")
        if len(source_pages) > 1 and ocr_page.get("preprocessed_key") != source_page["preprocessed_key"]:
            raise HTTPException(status_code=422, detail="OCR page image does not match preprocessing")
        image = _decode_image(source_page["preprocessed_key"])
        if image is None:
            raise HTTPException(status_code=422, detail=f"Preprocessed page {number} is unreadable")
        height, width = image.shape[:2]
        tokens = _normalize_ocr_tokens(ocr_page, width=width, height=height)
        blocks, backend = _detect_layout_blocks(image)
        if not blocks:
            blocks = _heuristic_blocks_from_tokens(tokens, width=width, height=height)
        page_blocks = _assign_tokens_to_blocks(blocks, tokens)
        for block in page_blocks:
            block["id"] = f"p{number:04d}-{block['id']}"
            block["page_number"] = number
            block["reading_order"] = len(enriched_blocks)
            enriched_blocks.append(block)
        pages.append({"page_number": number, "preprocessed_key": source_page["preprocessed_key"],
                      "backend": backend, "image_width": width, "image_height": height,
                      "block_count": len(page_blocks), "ocr_tokens_with_boxes": len(tokens), "blocks": page_blocks,
                      "full_text": "\n".join(block["text"] for block in page_blocks if block.get("text"))})
    full_text = "\n".join(block["text"] for block in enriched_blocks if block.get("text"))
    text_zone_count = sum(1 for block in enriched_blocks if block.get("text"))

    payload = {
        "job_id": request.job_id,
        "schema_version": SCHEMA_VERSION,
        "page_count": len(pages),
        "pages": pages,
        "backend": pages[0]["backend"] if len(pages) == 1 else "mixed",
        "layout_error": _LAYOUT_ERROR,
        "image_width": pages[0]["image_width"],
        "image_height": pages[0]["image_height"],
        "block_count": len(enriched_blocks),
        "text_zone_count": text_zone_count,
        "ocr_token_count": int(ocr_data.get("token_count") or len(tokens)),
        "ocr_tokens_with_boxes": sum(page["ocr_tokens_with_boxes"] for page in pages),
        "full_text": full_text,
        "blocks": enriched_blocks,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    key = artifact_key(request.tenant_id, request.job_id, "layout/layout.json")
    if request.page_slot is not None:
        if len(source_pages) != 1:
            raise HTTPException(422, "Independent layout requests must contain one page")
        key = artifact_key(request.tenant_id, request.job_id, f"layout/pages/{request.page_slot:04d}.json")
    payload["tenant_id"] = request.tenant_id
    payload["page_slot"] = request.page_slot
    upload_json(settings, settings.layout_bucket, key, payload)

    return {
        "job_id": request.job_id,
        "layout_bucket": settings.layout_bucket,
        "layout_key": key,
        "schema_version": SCHEMA_VERSION,
        "backend": payload["backend"],
        "page_count": len(pages),
        "duration_ms": payload["duration_ms"],
        "block_count": len(enriched_blocks),
        "text_zone_count": text_zone_count,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/layout")
async def analyze_layout(request: LayoutRequest) -> dict[str, Any]:
    if not _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="layout worker is busy")

    future = _LAYOUT_POOL.submit(_analyze_layout_sync, request)
    future.add_done_callback(_release_request_slot)
    timeout = max(1, int(settings.layout_request_timeout_seconds) * len(request.preprocessed_pages or [1]))
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="layout request timed out") from exc
