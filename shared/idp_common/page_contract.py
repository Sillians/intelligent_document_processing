from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from shared.idp_common.tenant_scope import require_artifact_scope


SCHEMA_VERSION = "2.0"


def validate_pages(
    pages: Any, *, tenant_id: str, job_id: str, key_field: str, first_key: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(pages, list) or not pages:
        raise HTTPException(status_code=422, detail="Document must contain at least one page")
    validated: list[dict[str, Any]] = []
    keys = set()
    for number, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or type(page.get("page_number")) is not int or page["page_number"] != number:
            raise HTTPException(status_code=422, detail="Pages must be ordered and numbered from one")
        key = page.get(key_field)
        if not isinstance(key, str) or not key:
            raise HTTPException(status_code=422, detail=f"Page {number} is missing {key_field}")
        require_artifact_scope(tenant_id, job_id, key)
        if key in keys:
            raise HTTPException(422, "Page artifact keys must be unique")
        keys.add(key)
        validated.append(page)
    if first_key is not None and validated[0][key_field] != first_key:
        raise HTTPException(status_code=422, detail="First-page key does not match page manifest")
    return validated
