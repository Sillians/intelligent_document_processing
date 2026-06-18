from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel, Field

from shared.idp_common.config import Settings


class EvaluationRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=64)
    ocr_confidence: float = Field(ge=0.0, le=1.0)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    used_vlm_fallback: bool
    requires_human_review: bool
    route: str | None = Field(default=None, max_length=64)
    validation_verdict: str | None = Field(default=None, max_length=64)
    field_count: int | None = Field(default=None, ge=0)
    populated_field_count: int | None = Field(default=None, ge=0)
    custom_metrics: dict[str, float] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    dataset_version: str | None = Field(default=None, max_length=128)
    pipeline_version: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=256)


@dataclass(frozen=True)
class EvaluationContext:
    settings: Settings
    request: EvaluationRequest
    evaluation_id: str
    metrics: dict[str, float]
    parameters: dict[str, str]
    tags: dict[str, str]

