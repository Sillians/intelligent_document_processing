from __future__ import annotations

import re

from fastapi import HTTPException


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def artifact_prefix(tenant_id: str, job_id: str) -> str:
    if not _IDENTIFIER.fullmatch(tenant_id) or not _IDENTIFIER.fullmatch(job_id):
        raise ValueError("Invalid tenant or job identifier")
    if tenant_id == "default":
        return f"jobs/{job_id}/"
    return f"tenants/{tenant_id}/jobs/{job_id}/"


def artifact_key(tenant_id: str, job_id: str, suffix: str) -> str:
    return artifact_prefix(tenant_id, job_id) + suffix


def require_artifact_scope(tenant_id: str, job_id: str, key: str) -> None:
    try:
        prefix = artifact_prefix(tenant_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not key.startswith(prefix) or any(part in {"", ".", ".."} for part in key.split("/")) or "\\" in key or "%" in key:
        raise HTTPException(status_code=403, detail="Artifact is outside the tenant/job scope")
