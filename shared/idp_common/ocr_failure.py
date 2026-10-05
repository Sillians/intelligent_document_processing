"""Stable OCR error codes shared by HTTP serving and workflow activities."""
from fastapi import HTTPException


def ocr_failure(reason: str) -> HTTPException:
    reason = reason.rsplit(":", 1)[-1]
    codes = {
        "invalid_image": ("OCR_INVALID_IMAGE", False),
        "no_text": ("OCR_NO_TEXT", False),
        "request_timeout": ("OCR_TIMEOUT", True),
        "busy": ("OCR_BUSY", True),
        "disabled": ("OCR_DISABLED", False),
        "secondary_rejected": ("OCR_SECONDARY_REJECTED", False),
    }
    code, retryable = codes.get(reason, ("OCR_UNAVAILABLE", True))
    return HTTPException(status_code=503 if retryable or reason == "disabled" else 422,
                         detail={"schema_version": "1.0", "code": code,
                                 "retryable": retryable, "stage": "ocr"})
