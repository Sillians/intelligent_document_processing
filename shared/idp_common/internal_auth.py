from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from shared.idp_common.config import get_settings


def internal_key_valid(value: str | None) -> bool:
    settings = get_settings()
    if not settings.idp_require_internal_auth:
        return True
    return bool(settings.idp_internal_api_key and value and hmac.compare_digest(value, settings.idp_internal_api_key))


def install_internal_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def require_internal_auth(request: Request, call_next):
        if request.url.path not in {"/health", "/ready", "/metrics", "/openapi.json"}:
            if not internal_key_valid(request.headers.get("X-IDP-Internal-Key")):
                return JSONResponse(status_code=401, content={"detail": "Invalid internal service credential"})
        return await call_next(request)
