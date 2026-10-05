"""Dedicated tenant-scoped operator credentials, separate from intake keys."""
import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from shared.idp_common.config import Settings, get_settings
from shared.idp_common.tenant_scope import artifact_prefix


@dataclass(frozen=True)
class Operator:
    tenant_id: str
    subject: str


async def resolve_operator(request: Request, settings: Settings = Depends(get_settings)) -> Operator:
    value = request.headers.get("X-IDP-Operator-Key", "")
    for entry in settings.idp_operator_api_keys.split(","):
        if ":" not in entry:
            continue
        key, tenant = (part.strip() for part in entry.split(":", 1))
        if key and value and hmac.compare_digest(key, value):
            try:
                artifact_prefix(tenant, "operator")
            except ValueError as exc:
                raise HTTPException(500, "Invalid configured operator tenant") from exc
            if request.headers.get("X-Tenant-Id", tenant) != tenant:
                raise HTTPException(403, "Operator tenant scope violation")
            return Operator(tenant, "operator:" + hashlib.sha256(key.encode()).hexdigest()[:16])
    raise HTTPException(401, "Invalid operator credential")
