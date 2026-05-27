from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "idp-service"

    postgres_host: str = Field(default="postgres", validation_alias=AliasChoices("POSTGRES_HOST"))
    postgres_port: int = Field(default=5432, validation_alias=AliasChoices("POSTGRES_PORT"))
    postgres_user: str = Field(default="idp", validation_alias=AliasChoices("POSTGRES_USER"))
    postgres_password: str = Field(default="idp_password", validation_alias=AliasChoices("POSTGRES_PASSWORD"))
    postgres_db: str = Field(default="idp", validation_alias=AliasChoices("POSTGRES_DB"))

    s3_endpoint: str = Field(
        default="seaweedfs:8333",
        validation_alias=AliasChoices("S3_ENDPOINT", "MINIO_ENDPOINT"),
    )
    s3_access_key: str = Field(
        default="idpadmin",
        validation_alias=AliasChoices("S3_ACCESS_KEY", "MINIO_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
    )
    s3_secret_key: str = Field(
        default="idpsecret123",
        validation_alias=AliasChoices("S3_SECRET_KEY", "MINIO_SECRET_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    s3_secure: bool = Field(
        default=False,
        validation_alias=AliasChoices("S3_SECURE", "MINIO_SECURE"),
    )

    raw_bucket: str = "raw-documents"
    preprocessed_bucket: str = "preprocessed-documents"
    ocr_bucket: str = "ocr-artifacts"
    layout_bucket: str = "layout-artifacts"
    extraction_bucket: str = "extraction-artifacts"
    delivery_bucket: str = "delivery-artifacts"

    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "idp-pipeline"
    orchestrator_http_timeout_seconds: int = Field(default=300, ge=1)
    orchestrator_http_connect_timeout_seconds: int = Field(default=10, ge=1)
    temporal_worker_identity: str = ""
    temporal_worker_max_cached_workflows: int = Field(default=1000, ge=1)
    temporal_worker_max_concurrent_workflow_tasks: int = Field(default=100, ge=1)
    temporal_worker_max_concurrent_activities: int = Field(default=200, ge=1)
    temporal_worker_graceful_shutdown_seconds: int = Field(default=15, ge=0)

    preprocess_url: str = "http://preprocess-worker:8010"
    ocr_url: str = "http://ocr-service:8011"
    layout_url: str = "http://layout-service:8012"
    classifier_url: str = "http://classifier-router-service:8013"
    extraction_url: str = "http://extraction-service:8014"
    validation_url: str = "http://validation-service:8015"
    review_url: str = "http://human-review-console:8016"
    delivery_url: str = "http://delivery-service:8017"
    evaluation_url: str = "http://evaluation-service:8018"

    ocr_confidence_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    vlm_fallback_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    auto_approve_threshold: float = Field(default=0.90, ge=0.0, le=1.0)

    vlm_base_url: str = "http://vllm:8000"
    vlm_model: str = "vlm-fallback"
    vlm_api_key: str = "EMPTY"

    label_studio_url: str = "http://label-studio:8080"
    label_studio_token: str = ""
    label_studio_project_id: int = 1

    mlflow_tracking_uri: str = "http://mlflow:5000"

    preprocess_max_dimension: int = Field(default=2200, ge=256)
    preprocess_denoise_h: int = Field(default=12, ge=0, le=50)
    preprocess_threshold_block_size: int = Field(default=35, ge=3)
    preprocess_threshold_c: int = Field(default=11, ge=-100, le=100)
    preprocess_enable_clahe: bool = True
    preprocess_deskew_min_foreground_pixels: int = Field(default=64, ge=0)
    ocr_disable_mkldnn: bool = True
    ocr_language: str = "en"
    ocr_force_fallback: bool = False
    ocr_engine_timeout_seconds: int = Field(default=60, ge=1)
    ocr_engine_lock_timeout_seconds: float = Field(default=1.5, gt=0)
    ocr_request_timeout_seconds: int = Field(default=90, ge=1)
    ocr_max_inflight_requests: int = Field(default=1, ge=1, le=8)
    orchestrator_enable_ocr_network_fallback: bool = True

    ingestion_max_upload_size_mb: int = Field(default=25, ge=1)
    ingestion_allowed_mime_types: str = (
        "application/pdf,image/png,image/jpeg,image/tiff,image/bmp,image/webp"
    )
    ingestion_allowed_extensions: str = "pdf,png,jpg,jpeg,tif,tiff,bmp,webp"
    ingestion_dedupe_window_hours: int = Field(default=24, ge=1)
    ingestion_workflow_timeout_minutes: int = Field(default=30, ge=1)

    ingestion_require_auth: bool = True
    ingestion_api_keys: str = "dev-ingestion-key:default"
    ingestion_auth_header_name: str = "x-api-key"
    ingestion_tenant_header_name: str = "x-tenant-id"
    ingestion_actor_header_name: str = "x-actor-id"

    ingestion_db_pool_min: int = Field(default=1, ge=1)
    ingestion_db_pool_max: int = Field(default=5, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
