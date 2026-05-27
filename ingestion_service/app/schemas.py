from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DocumentSubmissionResponse(BaseModel):
    job_id: str
    status: str
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    artifact_uri: str
    idempotency_replay: bool = False
    deduplicated: bool = False
    status_url: str


class DocumentStatusResponse(BaseModel):
    job_id: str
    tenant_id: str
    status: str
    source: str
    filename: str
    content_type: str
    size_bytes: int
    artifact_uri: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditEventResponse(BaseModel):
    id: int
    job_id: str | None
    tenant_id: str
    actor_id: str
    event_type: str
    event_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ListJobsResponse(BaseModel):
    jobs: list[DocumentStatusResponse]
