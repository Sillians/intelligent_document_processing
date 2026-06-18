from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
import logging
import math
import time
from threading import Lock
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.idp_common.config import get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import download_bytes, upload_bytes

settings = get_settings()
logger = logging.getLogger("preprocess_worker")
_PREPROCESS_MAX_INFLIGHT = max(1, int(getattr(settings, "preprocess_max_inflight_requests", 4)))
_PREPROCESS_POOL = ThreadPoolExecutor(max_workers=_PREPROCESS_MAX_INFLIGHT, thread_name_prefix="preprocess-worker")
_PREPROCESS_INFLIGHT = 0
_PREPROCESS_INFLIGHT_LOCK = Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _PREPROCESS_POOL.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="preprocess-worker", lifespan=lifespan)
instrument_app(app, "preprocess-worker")


class PreprocessRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    raw_bucket: str = Field(min_length=1, max_length=255)
    raw_key: str = Field(min_length=1, max_length=1024)


def _try_acquire_request_slot() -> bool:
    global _PREPROCESS_INFLIGHT
    with _PREPROCESS_INFLIGHT_LOCK:
        if _PREPROCESS_INFLIGHT >= _PREPROCESS_MAX_INFLIGHT:
            return False
        _PREPROCESS_INFLIGHT += 1
        return True


def _release_request_slot(_: Future[Any] | None = None) -> None:
    global _PREPROCESS_INFLIGHT
    with _PREPROCESS_INFLIGHT_LOCK:
        _PREPROCESS_INFLIGHT = max(0, _PREPROCESS_INFLIGHT - 1)


def _ensure_odd(value: int, *, minimum: int = 3) -> int:
    bounded = max(minimum, int(value))
    return bounded + 1 if bounded % 2 == 0 else bounded


def _prepare_grayscale(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.ndim != 3 or image.shape[2] not in {3, 4}:
        raise ValueError(f"Unsupported image shape: {image.shape}")
    if image.shape[2] == 4:
        image = image[:, :, :3]
    return np.clip(
        (0.114 * image[:, :, 0]) + (0.587 * image[:, :, 1]) + (0.299 * image[:, :, 2]),
        0,
        255,
    ).astype(np.uint8)


def _normalize_to_bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.repeat(image[:, :, np.newaxis], 3, axis=2)
    if image.ndim == 3 and image.shape[2] == 4:
        return image[:, :, :3]
    if image.ndim == 3 and image.shape[2] == 3:
        return image
    raise ValueError(f"Unsupported image shape: {image.shape}")


def _resize_if_needed(image: np.ndarray, *, max_dimension: int) -> tuple[np.ndarray, float]:
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_dimension:
        return image, 1.0
    scale = float(max_dimension) / float(longest)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized, scale


def _estimate_skew_angle(gray: np.ndarray, *, min_foreground_pixels: int) -> float:
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(mask > 0))
    if coords.shape[0] < min_foreground_pixels:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45.0:
        angle = 90.0 + angle
    angle = -float(angle)
    return max(-45.0, min(45.0, angle))


def _rotate(image: np.ndarray, angle: float) -> np.ndarray:
    h, w = image.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(image, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _guess_content_type(payload: bytes) -> str:
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return "application/octet-stream"


def _safe_reason(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200]


def _foreground_pixel_count(gray: np.ndarray) -> int:
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return int(np.count_nonzero(mask))


def _encode_png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("OpenCV could not encode processed image")
    return encoded.tobytes()


def preprocess_image(
    image: np.ndarray,
    *,
    max_dimension: int,
    denoise_h: int,
    threshold_block_size: int,
    threshold_c: int,
    enable_clahe: bool,
    min_foreground_pixels: int,
    enable_deskew: bool = True,
    enable_threshold: bool = True,
    median_blur_kernel: int = 3,
    clahe_clip_limit: float = 2.0,
    clahe_tile_grid_size: int = 8,
) -> tuple[np.ndarray, dict[str, Any]]:
    original_height, original_width = image.shape[:2]
    resized, resize_scale = _resize_if_needed(image, max_dimension=max_dimension)
    gray = _prepare_grayscale(resized)
    steps = ["resize" if not math.isclose(resize_scale, 1.0) else "resize_skipped", "grayscale"]

    denoised = cv2.fastNlMeansDenoising(gray, None, h=max(0, denoise_h))
    steps.append("denoise")
    working = denoised

    if enable_clahe:
        tile_size = max(1, int(clahe_tile_grid_size))
        clahe = cv2.createCLAHE(clipLimit=float(clahe_clip_limit), tileGridSize=(tile_size, tile_size))
        working = clahe.apply(denoised)
        steps.append("clahe")

    foreground_pixels = _foreground_pixel_count(working)
    angle = 0.0
    if enable_deskew:
        angle = _estimate_skew_angle(working, min_foreground_pixels=max(0, min_foreground_pixels))
        working = _rotate(working, angle) if not math.isclose(angle, 0.0, abs_tol=0.25) else working
        steps.append("deskew" if not math.isclose(angle, 0.0, abs_tol=0.25) else "deskew_skipped")

    block_size = _ensure_odd(threshold_block_size, minimum=3)
    if enable_threshold:
        working = cv2.adaptiveThreshold(
            working,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size,
            int(threshold_c),
        )
        steps.append("adaptive_threshold")

    blur_kernel = _ensure_odd(median_blur_kernel, minimum=1)
    if blur_kernel > 1:
        working = cv2.medianBlur(working, blur_kernel)
        steps.append("median_blur")

    cleaned = working
    result = cv2.cvtColor(cleaned, cv2.COLOR_GRAY2BGR)

    metadata = {
        "deskew_angle": round(angle, 4),
        "resize_scale": round(float(resize_scale), 6),
        "original_width": int(original_width),
        "original_height": int(original_height),
        "width": int(result.shape[1]),
        "height": int(result.shape[0]),
        "foreground_pixels": foreground_pixels,
        "threshold_block_size": block_size,
        "median_blur_kernel": blur_kernel,
        "pipeline": steps,
    }
    return result, metadata


def _preprocess_sync(request: PreprocessRequest) -> dict[str, Any]:
    started = time.perf_counter()
    raw = download_bytes(settings, request.raw_bucket, request.raw_key)
    arr = np.frombuffer(raw, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)

    metadata: dict[str, Any]
    content_type = "image/png"
    key = f"jobs/{request.job_id}/preprocessed/page-0001.png"

    if image is None:
        out = raw
        content_type = _guess_content_type(raw)
        key = f"jobs/{request.job_id}/preprocessed/source.bin"
        metadata = {
            "pipeline": ["passthrough"],
            "reason": "unsupported_format",
            "content_type": content_type,
            "size_bytes": len(raw),
        }
    else:
        try:
            processed, metadata = preprocess_image(
                image,
                max_dimension=int(getattr(settings, "preprocess_max_dimension", 2200)),
                denoise_h=int(getattr(settings, "preprocess_denoise_h", 12)),
                threshold_block_size=int(getattr(settings, "preprocess_threshold_block_size", 35)),
                threshold_c=int(getattr(settings, "preprocess_threshold_c", 11)),
                enable_clahe=bool(getattr(settings, "preprocess_enable_clahe", True)),
                min_foreground_pixels=int(getattr(settings, "preprocess_deskew_min_foreground_pixels", 64)),
                enable_deskew=bool(getattr(settings, "preprocess_enable_deskew", True)),
                enable_threshold=bool(getattr(settings, "preprocess_enable_threshold", True)),
                median_blur_kernel=int(getattr(settings, "preprocess_median_blur_kernel", 3)),
                clahe_clip_limit=float(getattr(settings, "preprocess_clahe_clip_limit", 2.0)),
                clahe_tile_grid_size=int(getattr(settings, "preprocess_clahe_tile_grid_size", 8)),
            )
            out = _encode_png(processed)
        except Exception as exc:  # noqa: BLE001
            logger.exception("preprocess pipeline failed job_id=%s", request.job_id, exc_info=exc)
            try:
                out = _encode_png(_normalize_to_bgr(image))
                metadata = {"pipeline": ["fallback_original_image"], "reason": _safe_reason(exc)}
            except Exception as fallback_exc:  # noqa: BLE001
                out = raw
                content_type = _guess_content_type(raw)
                key = f"jobs/{request.job_id}/preprocessed/source.bin"
                metadata = {
                    "pipeline": ["passthrough"],
                    "reason": "preprocess_and_encode_failed",
                    "error": _safe_reason(fallback_exc),
                }

    metadata["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    upload_bytes(settings, settings.preprocessed_bucket, key, out, content_type)
    return {
        "job_id": request.job_id,
        "preprocessed_bucket": settings.preprocessed_bucket,
        "preprocessed_key": key,
        "metadata": metadata,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/preprocess")
async def preprocess(request: PreprocessRequest) -> dict:
    if not _try_acquire_request_slot():
        raise HTTPException(status_code=503, detail="preprocess worker is busy")

    future = _PREPROCESS_POOL.submit(_preprocess_sync, request)
    future.add_done_callback(_release_request_slot)
    timeout = max(1, int(getattr(settings, "preprocess_request_timeout_seconds", 60)))
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="preprocess request timed out") from exc
