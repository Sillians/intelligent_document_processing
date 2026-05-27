from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from prometheus_client import Counter

from shared.idp_common.config import Settings, get_settings


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    auth_scheme: str


INGESTION_AUTH_FAILURES = Counter(
    "ingestion_auth_failures_total",
    "Authentication failures by reason",
    ["reason"],
)


def _parse_api_key_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not raw.strip():
        return mapping

    for pair in raw.split(","):
        cleaned = pair.strip()
        if not cleaned:
            continue
        if ":" not in cleaned:
            continue
        key, tenant = cleaned.split(":", 1)
        key = key.strip()
        tenant = tenant.strip()
        if key and tenant:
            mapping[key] = tenant
    return mapping


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _resolve_header_case_insensitive(header_name: str, values: dict[str, str]) -> str | None:
    if header_name in values:
        return values[header_name]

    lower_name = header_name.lower()
    for name, value in values.items():
        if name.lower() == lower_name:
            return value
    return None


async def resolve_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal:
    headers = dict(request.headers.items())
    authorization = _resolve_header_case_insensitive("authorization", headers)
    tenant_header = _resolve_header_case_insensitive(settings.ingestion_tenant_header_name, headers)
    actor_header = _resolve_header_case_insensitive(settings.ingestion_actor_header_name, headers)

    if not settings.ingestion_require_auth:
        tenant_id = tenant_header or "default"
        subject = actor_header or "anonymous"
        return Principal(tenant_id=tenant_id, subject=subject, auth_scheme="none")

    api_key_value = _resolve_header_case_insensitive(settings.ingestion_auth_header_name, headers)

    token = api_key_value or _extract_bearer_token(authorization)
    if not token:
        INGESTION_AUTH_FAILURES.labels("missing_credentials").inc()
        raise HTTPException(status_code=401, detail="Missing API credentials")

    key_map = _parse_api_key_map(settings.ingestion_api_keys)
    tenant_id = key_map.get(token)
    if not tenant_id:
        INGESTION_AUTH_FAILURES.labels("invalid_credentials").inc()
        raise HTTPException(status_code=401, detail="Invalid API credentials")

    requested_tenant = tenant_header
    if requested_tenant and requested_tenant != tenant_id:
        INGESTION_AUTH_FAILURES.labels("tenant_scope_violation").inc()
        raise HTTPException(status_code=403, detail="Tenant scope violation")

    subject = actor_header or f"api-key:{tenant_id}"
    return Principal(tenant_id=tenant_id, subject=subject, auth_scheme="api-key")
