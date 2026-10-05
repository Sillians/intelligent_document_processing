from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import Future, ThreadPoolExecutor
import logging
import os
import re
import time
from statistics import mean
from threading import Lock
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from shared.idp_common.ocr_failure import ocr_failure
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_bytes, upload_json
from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope
from shared.idp_common.internal_auth import install_internal_auth
from shared.idp_common.page_contract import SCHEMA_VERSION, validate_pages

settings = get_settings()
logger = logging.getLogger("ocr_service")

_OCR_ENGINE = None
_OCR_INIT_ERROR = None
_OCR_ENGINE_CONFIG: dict[str, Any] | None = None
_OCR_ENGINE_LOCK = Lock()
_OCR_MAX_INFLIGHT = max(1, int(getattr(settings, "ocr_max_inflight_requests", 1)))
_OCR_REQUEST_POOL = ThreadPoolExecutor(max_workers=_OCR_MAX_INFLIGHT, thread_name_prefix="ocr-worker")
_OCR_INFLIGHT = 0
_OCR_INFLIGHT_LOCK = Lock()


def _configured_backend() -> str:
    if bool(getattr(settings, "ocr_force_fallback", False)):
        return "fallback"
    backend = str(getattr(settings, "ocr_backend", "paddle") or "paddle").strip().lower()
    if backend not in {"paddle", "tesseract", "fallback"}:
        raise RuntimeError(f"Unsupported OCR_BACKEND: {backend}")
    return backend


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _configured_backend() != "fallback":
        try:
            await asyncio.to_thread(get_ocr_engine)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR engine failed to initialize at startup", exc_info=exc)

    yield

    _OCR_REQUEST_POOL.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="ocr-service", lifespan=lifespan)
instrument_app(app, "ocr-service")
install_internal_auth(app)


class OCRRequest(BaseModel):
    page_slot: int | None = Field(default=None, ge=1, le=1000)
    tenant_id: str = "default"
    job_id: str
    preprocessed_key: str
    preprocessed_pages: list[dict[str, Any]] | None = None


def _try_acquire_request_slot() -> bool:
    global _OCR_INFLIGHT
    with _OCR_INFLIGHT_LOCK:
        if _OCR_INFLIGHT >= _OCR_MAX_INFLIGHT:
            return False
        _OCR_INFLIGHT += 1
        return True


def _release_request_slot(_: Future[Any] | None = None) -> None:
    global _OCR_INFLIGHT
    with _OCR_INFLIGHT_LOCK:
        if _OCR_INFLIGHT > 0:
            _OCR_INFLIGHT -= 1


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    return re.sub(r"\s+", " ", text).strip()


def _to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        converted = value.tolist()
        return converted if isinstance(converted, list) else [converted]
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_tokens_from_legacy_page(page_result: list[Any]) -> tuple[list[dict[str, Any]], list[float]]:
    tokens: list[dict[str, Any]] = []
    confidences: list[float] = []
    for item in page_result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        box = _to_list(item[0])
        text_data = item[1]
        text = ""
        score = 0.0
        if isinstance(text_data, (list, tuple)):
            if len(text_data) > 0:
                text = _normalize_text(text_data[0])
            if len(text_data) > 1:
                score = _as_float(text_data[1], 0.0)
        else:
            text = _normalize_text(text_data)

        if text:
            tokens.append({"text": text, "bbox": box, "confidence": score})
            confidences.append(score)
    return tokens, confidences


def _extract_tokens_from_v3_page(page_result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[float]]:
    tokens: list[dict[str, Any]] = []
    confidences: list[float] = []

    texts = _to_list(page_result.get("rec_texts"))
    scores = _to_list(page_result.get("rec_scores"))
    boxes = _to_list(page_result.get("rec_polys") or page_result.get("dt_polys"))

    for idx, raw_text in enumerate(texts):
        text = _normalize_text(raw_text)
        if not text:
            continue
        score = _as_float(scores[idx], 0.0) if idx < len(scores) else 0.0
        box = _to_list(boxes[idx]) if idx < len(boxes) else []
        tokens.append({"text": text, "bbox": box, "confidence": score})
        confidences.append(score)

    return tokens, confidences


def _run_engine(engine: Any, image: Any) -> Any:
    method_attempts = [
        lambda: engine.predict(image),
        lambda: engine.ocr(image),
        lambda: engine.ocr(image, cls=True),
    ]
    last_error: Exception | None = None
    for attempt in method_attempts:
        try:
            return attempt()
        except TypeError:
            # Try next API style for cross-version PaddleOCR compatibility.
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            break
    if last_error is not None:
        raise last_error
    raise RuntimeError("No compatible PaddleOCR inference method available")


def _extract_tokens(result: Any) -> tuple[list[dict[str, Any]], list[float]]:
    if isinstance(result, dict):
        return _extract_tokens_from_v3_page(result)

    if not isinstance(result, list):
        return [], []

    tokens: list[dict[str, Any]] = []
    confidences: list[float] = []

    for page in result:
        page_tokens: list[dict[str, Any]]
        page_confidences: list[float]
        if isinstance(page, dict):
            page_tokens, page_confidences = _extract_tokens_from_v3_page(page)
        elif isinstance(page, list):
            page_tokens, page_confidences = _extract_tokens_from_legacy_page(page)
        else:
            continue

        tokens.extend(page_tokens)
        confidences.extend(page_confidences)

    return tokens, confidences


def _build_engine_candidates() -> list[dict[str, Any]]:
    lang = str(getattr(settings, "ocr_language", "en") or "en")
    disable_mkldnn = bool(getattr(settings, "ocr_disable_mkldnn", True))

    candidates: list[dict[str, Any]] = []
    if disable_mkldnn:
        candidates.append({"lang": lang, "use_textline_orientation": True, "enable_mkldnn": False})
        candidates.append({"lang": lang, "use_angle_cls": True, "enable_mkldnn": False})
    candidates.append({"lang": lang, "use_textline_orientation": True})
    candidates.append({"lang": lang, "use_angle_cls": True})
    candidates.append({"lang": lang})
    return candidates


class TesseractEngine:
    def __init__(self, pytesseract_module: Any, *, language: str, oem: int, psm: int):
        self._pytesseract = pytesseract_module
        self._language = language
        self._config = f"--oem {oem} --psm {psm}"

    def predict(self, image: Any) -> list[dict[str, Any]]:
        data = self._pytesseract.image_to_data(
            image,
            lang=self._language,
            config=self._config,
            output_type=self._pytesseract.Output.DICT,
        )
        texts: list[str] = []
        scores: list[float] = []
        boxes: list[list[list[int]]] = []
        item_count = len(data.get("text", []))
        for index in range(item_count):
            text = _normalize_text(data["text"][index])
            score = _as_float(data.get("conf", [])[index] if index < len(data.get("conf", [])) else -1.0, -1.0)
            if not text or score < 0.0:
                continue
            left = int(_as_float(data.get("left", [])[index], 0.0))
            top = int(_as_float(data.get("top", [])[index], 0.0))
            width = int(_as_float(data.get("width", [])[index], 0.0))
            height = int(_as_float(data.get("height", [])[index], 0.0))
            texts.append(text)
            scores.append(max(0.0, min(1.0, score / 100.0)))
            boxes.append(
                [
                    [left, top],
                    [left + width, top],
                    [left + width, top + height],
                    [left, top + height],
                ]
            )
        return [{"rec_texts": texts, "rec_scores": scores, "rec_polys": boxes}]


def _init_tesseract_engine() -> tuple[Any, dict[str, Any]]:
    import pytesseract

    language = str(getattr(settings, "ocr_language", "eng") or "eng")
    # Paddle uses "en"; Tesseract's equivalent trained-data identifier is "eng".
    if language == "en":
        language = "eng"
    oem = int(getattr(settings, "ocr_tesseract_oem", 1))
    psm = int(getattr(settings, "ocr_tesseract_psm", 3))
    version = str(pytesseract.get_tesseract_version())
    return (
        TesseractEngine(pytesseract, language=language, oem=oem, psm=psm),
        {
            "backend": "tesseract",
            "language": language,
            "oem": oem,
            "psm": psm,
            "version": version,
        },
    )


def _init_paddle_engine() -> tuple[Any, dict[str, Any]]:
    from paddleocr import PaddleOCR

    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_enable_pir_api", "0")

    last_error: Exception | None = None
    for kwargs in _build_engine_candidates():
        try:
            engine = PaddleOCR(**kwargs)
            return engine, {"backend": "paddle", **kwargs}
        except TypeError:
            # Ignore unsupported init args for cross-version compatibility.
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to initialize PaddleOCR with any supported configuration")


def _init_ocr_engine() -> tuple[Any, dict[str, Any]]:
    backend = _configured_backend()
    if backend == "tesseract":
        return _init_tesseract_engine()
    if backend == "paddle":
        return _init_paddle_engine()
    raise RuntimeError("OCR fallback mode does not initialize an engine")


def get_ocr_engine():
    global _OCR_ENGINE, _OCR_INIT_ERROR, _OCR_ENGINE_CONFIG
    if _OCR_ENGINE is not None or _OCR_INIT_ERROR is not None:
        return _OCR_ENGINE

    lock_timeout = float(getattr(settings, "ocr_engine_lock_timeout_seconds", 1.5))
    acquired = _OCR_ENGINE_LOCK.acquire(timeout=max(0.1, lock_timeout))
    if not acquired:
        raise RuntimeError("ocr_engine_lock_timeout")

    try:
        if _OCR_ENGINE is not None or _OCR_INIT_ERROR is not None:
            return _OCR_ENGINE
        try:
            _OCR_ENGINE, _OCR_ENGINE_CONFIG = _init_ocr_engine()
        except Exception as exc:  # noqa: BLE001
            _OCR_INIT_ERROR = str(exc)
            _OCR_ENGINE = None
    finally:
        _OCR_ENGINE_LOCK.release()

    return _OCR_ENGINE


def _load_and_decode_image(preprocessed_key: str) -> np.ndarray | None:
    raw = download_bytes(settings, settings.preprocessed_bucket, preprocessed_key)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)


def _normalize_input_image(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.repeat(image[:, :, np.newaxis], 3, axis=2)
    if image.ndim == 3 and image.shape[2] == 4:
        return image[:, :, :3]
    return image


def _execute_ocr_sync(job_id: str, preprocessed_key: str) -> tuple[list[dict[str, Any]], list[float], str]:
    try:
        image = _load_and_decode_image(preprocessed_key)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR artifact download/decode failed job_id=%s", job_id, exc_info=exc)
        return [], [], "artifact_read_failed"

    if image is None:
        return [], [], "invalid_image"

    if _configured_backend() == "fallback":
        return [], [], "forced_fallback"

    engine = None
    try:
        engine = get_ocr_engine()
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR engine initialization failed job_id=%s", job_id, exc_info=exc)
        return [], [], "engine_init_failed"

    if engine is None:
        if _OCR_INIT_ERROR:
            return [], [], _OCR_INIT_ERROR
        return [], [], "engine_unavailable"

    try:
        normalized = _normalize_input_image(image)
        result = _run_engine(engine, normalized)
        tokens, confidences = _extract_tokens(result)
        return tokens, confidences, ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR engine execution failed job_id=%s", job_id, exc_info=exc)
        return [], [], "engine_execution_failed"


def _execute_document_ocr(job_id: str, pages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    results = []
    for page in pages:
        tokens, confidences, reason = _execute_ocr_sync(job_id, page["preprocessed_key"])
        primary_failure = ocr_failure(reason).detail["code"] if reason else None
        secondary_config = None
        if reason and reason not in {"invalid_image", "forced_fallback", "artifact_read_failed"} and settings.ocr_secondary_backend == "tesseract" and _configured_backend() != "tesseract":
            try:
                image = _load_and_decode_image(page["preprocessed_key"])
                if image is None:
                    return [], "invalid_image"
                engine, secondary_config = _init_tesseract_engine()
                tokens, confidences = _extract_tokens(_run_engine(engine, _normalize_input_image(image)))
                if not valid_secondary_output(tokens, confidences, image.shape):
                    return [], "secondary_rejected"
                reason = ""
            except Exception:
                logger.exception("Secondary OCR failed job_id=%s", job_id)
                return [], "secondary_failed"
        if reason:
            return [], f"page_{page['page_number']}:{reason}"
        results.append({"page_number": page["page_number"], "preprocessed_key": page["preprocessed_key"], "tokens": tokens, "confidences": confidences, "secondary_engine": secondary_config, "primary_failure": primary_failure})
    return results, ""


def valid_secondary_output(tokens, confidences, shape):
    import math
    if not tokens or len(tokens) != len(confidences):
        return False
    if any(not math.isfinite(score) or score < 0 or score > 1 for score in confidences):
        return False
    if mean(confidences) < settings.ocr_secondary_min_confidence:
        return False
    height, width = shape[:2]
    for token in tokens:
        if not str(token.get("text") or "").strip():
            return False
        box = token.get("bbox")
        if not isinstance(box, list) or len(box) != 4:
            return False
        for point in box:
            if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in point) or not (0 <= point[0] <= width and 0 <= point[1] <= height):
                return False
        if max(point[0] for point in box) <= min(point[0] for point in box) or max(point[1] for point in box) <= min(point[1] for point in box):
            return False
    return True


def _build_response(
    *,
    job_id: str,
    tenant_id: str = "default",
    tokens: list[dict[str, Any]],
    confidences: list[float],
    fallback_used: bool,
    pages: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    mean_conf = round(mean(confidences), 4) if confidences else 0.0
    full_text = "\n".join(token.get("text", "") for token in tokens if token.get("text"))

    artifact = {
        "job_id": job_id,
        "tokens": tokens,
        "token_count": len(tokens),
        "mean_confidence": mean_conf,
        "full_text": full_text,
        "engine_config": _OCR_ENGINE_CONFIG or {},
        "engine_backend": (_OCR_ENGINE_CONFIG or {}).get("backend", _configured_backend()),
        "fallback_used": fallback_used,
        "schema_version": SCHEMA_VERSION,
        "page_count": len(pages) if pages else 1,
        "pages": pages or [{"page_number": 1, "tokens": tokens, "token_count": len(tokens), "mean_confidence": mean_conf, "full_text": full_text}],
    }
    key = artifact_key(tenant_id, job_id, "ocr/ocr.json")

    response = {
        "job_id": job_id,
        "ocr_bucket": settings.ocr_bucket,
        "ocr_key": key,
        "token_count": len(tokens),
        "mean_confidence": mean_conf,
        "full_text": full_text,
        "engine_backend": (_OCR_ENGINE_CONFIG or {}).get("backend", _configured_backend()),
        "fallback_used": fallback_used,
        "schema_version": SCHEMA_VERSION,
        "page_count": len(pages) if pages else 1,
    }
    return artifact, response, key


@app.get("/health")
async def health() -> dict[str, Any]:
    configured_backend = _configured_backend()
    return {
        "status": "ok",
        "configured_backend": configured_backend,
        "active_backend": (_OCR_ENGINE_CONFIG or {}).get("backend", ""),
        "engine_ready": _OCR_ENGINE is not None,
        "fallback_forced": configured_backend == "fallback",
        "initialization_error": bool(_OCR_INIT_ERROR),
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    backend = _configured_backend()
    if backend == "fallback":
        raise ocr_failure("disabled")
    if _OCR_ENGINE is None:
        if settings.ocr_secondary_backend == "tesseract" and backend != "tesseract":
            try:
                _, secondary = await asyncio.to_thread(_init_tesseract_engine)
                return {"status": "degraded", "configured_backend": backend,
                        "active_backend": "tesseract", "secondary_engine": secondary}
            except Exception:
                logger.exception("Secondary OCR readiness check failed")
        reason = _OCR_INIT_ERROR or "OCR engine is not initialized"
        raise HTTPException(status_code=503, detail=f"{backend} backend unavailable: {reason}")
    return {"status": "ready", "backend": (_OCR_ENGINE_CONFIG or {}).get("backend", backend)}


@app.post("/ocr")
async def run_ocr(request: OCRRequest) -> dict:
    started = time.perf_counter()
    require_artifact_scope(request.tenant_id, request.job_id, request.preprocessed_key)
    source_pages = validate_pages(
        request.preprocessed_pages or [{"page_number": 1, "preprocessed_key": request.preprocessed_key}],
        tenant_id=request.tenant_id, job_id=request.job_id, key_field="preprocessed_key", first_key=request.preprocessed_key,
    )
    if request.page_slot is not None and len(source_pages) != 1:
        raise HTTPException(422, "Independent OCR requests must contain one page")
    tokens: list[dict[str, Any]] = []
    confidences: list[float] = []
    fallback_reason = "runtime"
    fallback_used = False
    per_page_timeout = max(
        1,
        int(getattr(settings, "ocr_request_timeout_seconds", getattr(settings, "ocr_engine_timeout_seconds", 60))),
    )
    request_timeout = per_page_timeout * len(source_pages)

    if _configured_backend() == "fallback":
        raise ocr_failure("disabled")

    if _try_acquire_request_slot():
        request_future = _OCR_REQUEST_POOL.submit(_execute_document_ocr, request.job_id, source_pages)
        request_future.add_done_callback(_release_request_slot)

        try:
            async_future = asyncio.wrap_future(request_future)
            page_results, fallback_reason = await asyncio.wait_for(
                asyncio.shield(async_future),
                timeout=request_timeout,
            )
            tokens = [{**token, "page_number": page["page_number"]} for page in page_results for token in page["tokens"]]
            confidences = [score for page in page_results for score in page["confidences"]]
        except asyncio.TimeoutError:
            logger.error("OCR request timed out job_id=%s", request.job_id)
            tokens = []
            confidences = []
            fallback_reason = "request_timeout"
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR request failed job_id=%s", request.job_id, exc_info=exc)
            tokens = []
            confidences = []
            fallback_reason = "request_failed"
    else:
        raise ocr_failure("busy")

    if not tokens:
        if fallback_reason:
            raise ocr_failure(fallback_reason)
        raise ocr_failure("no_text")

    page_payloads = [
        {"page_number": page["page_number"], "preprocessed_key": page["preprocessed_key"],
         "tokens": [{**token, "page_number": page["page_number"]} for token in page["tokens"]],
         "secondary_engine": page.get("secondary_engine"),
         "primary_failure": page.get("primary_failure"),
         "token_count": len(page["tokens"]),
         "mean_confidence": round(mean(page["confidences"]), 4) if page["confidences"] else 0.0,
         "full_text": "\n".join(str(token.get("text") or "") for token in page["tokens"] if token.get("text"))}
        for page in page_results
    ]

    artifact, response, key = _build_response(
        job_id=request.job_id,
        tenant_id=request.tenant_id,
        tokens=tokens,
        confidences=confidences,
        fallback_used=any(page.get("secondary_engine") for page in page_results),
        pages=page_payloads,
    )
    if request.page_slot is not None:
        key = artifact_key(request.tenant_id, request.job_id, f"ocr/pages/{request.page_slot:04d}.json")
        response["ocr_key"] = key
    artifact["tenant_id"] = request.tenant_id
    artifact["page_slot"] = request.page_slot
    secondary_pages = [page for page in page_payloads if page.get("secondary_engine")]
    if secondary_pages:
        backend = "tesseract" if len(secondary_pages) == len(page_payloads) else "mixed"
        artifact["engine_backend"] = response["engine_backend"] = backend
        artifact["engine_config"] = {"primary": _OCR_ENGINE_CONFIG or {},
                                   "secondary": secondary_pages[0]["secondary_engine"]}
    await asyncio.to_thread(upload_json, settings, settings.ocr_bucket, key, artifact)
    response["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return response
