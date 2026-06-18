from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel, Field

from shared.idp_common.config import Settings


class DeliveryRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]
    approval_status: str | None = Field(default=None, max_length=64)
    destinations: list[str] | None = None
    webhook_url: str | None = Field(default=None, max_length=2048)
    idempotency_key: str | None = Field(default=None, max_length=256)
    extraction_bucket: str | None = Field(default=None, max_length=128)
    extraction_key: str | None = Field(default=None, max_length=1024)
    validation_bucket: str | None = Field(default=None, max_length=128)
    validation_key: str | None = Field(default=None, max_length=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    workflow_id: str | None = Field(default=None, max_length=256)
    event_id: str | None = Field(default=None, max_length=256)
    occurred_at: str | None = Field(default=None, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    webhook_url: str | None = Field(default=None, max_length=2048)
    idempotency_key: str | None = Field(default=None, max_length=256)


@dataclass(frozen=True)
class DeliveryContext:
    settings: Settings
    request: DeliveryRequest
    delivery_id: str
    envelope: dict[str, Any]
