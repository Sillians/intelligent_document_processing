#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


PLACEHOLDER_PATTERN = re.compile(r"(changeme|replace-with|dev-|example\.com|idp_password|idpsecret123|idpadmin)", re.I)
SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "INGESTION_API_KEYS",
    "VLLM_API_KEY",
    "LABEL_STUDIO_TOKEN",
    "LABEL_STUDIO_PASSWORD",
    "DELIVERY_WEBHOOK_SECRET",
    "GRAFANA_ADMIN_PASSWORD",
    "ALERT_EMAIL_PASSWORD",
    "BACKUP_ENCRYPTION_KEY",
}
REQUIRED_KEYS = {
    "ENVIRONMENT",
    "PUBLIC_BASE_URL",
    "POSTGRES_PASSWORD",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "GATEWAY_HTTPS_HOST_PORT",
    "INGESTION_REQUIRE_AUTH",
    "INGESTION_API_KEYS",
    "LABEL_STUDIO_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "INGESTION_KEYS_ROTATED_AT",
    "DATABASE_CREDENTIALS_ROTATED_AT",
    "CREDENTIAL_MAX_AGE_DAYS",
    "RAW_ARTIFACT_RETENTION_DAYS",
    "DERIVED_ARTIFACT_RETENTION_DAYS",
    "AUDIT_RETENTION_DAYS",
    "BACKUP_RETENTION_DAYS",
    "BACKUP_ENCRYPTION_ENABLED",
    "BACKUP_ENCRYPTION_KEY",
    "INFRASTRUCTURE_PROFILE",
    "OCR_BACKEND",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=value")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def is_false(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


def is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def positive_int(values: dict[str, str], key: str, failures: list[str]) -> int | None:
    try:
        parsed = int(values.get(key, ""))
    except ValueError:
        failures.append(f"{key} must be a positive integer")
        return None
    if parsed < 1:
        failures.append(f"{key} must be a positive integer")
        return None
    return parsed


def check_rotation_age(
    values: dict[str, str],
    key: str,
    max_age_days: int | None,
    now: datetime,
    failures: list[str],
) -> None:
    raw_value = values.get(key, "")
    if not raw_value or max_age_days is None:
        return
    try:
        rotated_at = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        failures.append(f"{key} must be an ISO-8601 timestamp")
        return
    if rotated_at.tzinfo is None:
        rotated_at = rotated_at.replace(tzinfo=UTC)
    age_days = (now - rotated_at.astimezone(UTC)).total_seconds() / 86400
    if age_days < -1:
        failures.append(f"{key} cannot be in the future")
    elif age_days > max_age_days:
        failures.append(f"{key} is older than CREDENTIAL_MAX_AGE_DAYS={max_age_days}")


def check_env(
    values: dict[str, str],
    *,
    environment: str = "production",
    now: datetime | None = None,
) -> list[str]:
    failures: list[str] = []
    now = now or datetime.now(UTC)

    for key in sorted(REQUIRED_KEYS):
        if not values.get(key):
            failures.append(f"{key} is required")

    public_base_url = values.get("PUBLIC_BASE_URL", "")
    if public_base_url and PLACEHOLDER_PATTERN.search(public_base_url):
        failures.append("PUBLIC_BASE_URL must be set to the real production URL")
    parsed_public_url = urlparse(public_base_url)
    if environment == "production" and parsed_public_url.scheme != "https":
        failures.append("PUBLIC_BASE_URL must use https in production")

    configured_environment = values.get("ENVIRONMENT", "").lower()
    if configured_environment != environment:
        failures.append(f"ENVIRONMENT must be {environment}")

    if is_false(values.get("INGESTION_REQUIRE_AUTH", "")):
        failures.append("INGESTION_REQUIRE_AUTH must be true in production")

    api_keys = values.get("INGESTION_API_KEYS", "")
    parsed_keys: list[tuple[str, str]] = []
    if api_keys:
        for entry in api_keys.split(","):
            if ":" not in entry:
                failures.append("INGESTION_API_KEYS entries must use api-key:tenant-id format")
                continue
            api_key, tenant = entry.split(":", 1)
            if len(api_key.strip()) < 24:
                failures.append("INGESTION_API_KEYS must use high-entropy keys of at least 24 characters")
            if not tenant.strip():
                failures.append("INGESTION_API_KEYS entries must include a tenant id")
            parsed_keys.append((api_key.strip(), tenant.strip()))
    if len({api_key for api_key, _ in parsed_keys}) != len(parsed_keys):
        failures.append("INGESTION_API_KEYS must not contain duplicate API keys")

    for key in sorted(SECRET_KEYS):
        value = values.get(key, "")
        if not value:
            continue
        if PLACEHOLDER_PATTERN.search(value):
            failures.append(f"{key} still looks like a development default or placeholder")
        if key.endswith(("PASSWORD", "SECRET", "SECRET_KEY")) and len(value) < 24:
            failures.append(f"{key} should be at least 24 characters")

    if values.get("GRAFANA_ADMIN_USER", "").lower() == "admin" and values.get("GRAFANA_ADMIN_PASSWORD", "").lower() == "admin":
        failures.append("Grafana admin/admin is not allowed in production")

    if is_false(values.get("DELIVERY_WEBHOOK_REQUIRE_SIGNATURE", "true")):
        failures.append("DELIVERY_WEBHOOK_REQUIRE_SIGNATURE should stay true in production")

    if values.get("DELIVERY_ALLOW_REQUEST_WEBHOOK_URL", "false").lower() == "true":
        failures.append("DELIVERY_ALLOW_REQUEST_WEBHOOK_URL should stay false unless SSRF controls are reviewed")

    if values.get("EXTRACTION_ENABLE_VLM_FALLBACK", "false").lower() == "true":
        vlm_api_key = values.get("VLLM_API_KEY", "")
        if not vlm_api_key or vlm_api_key.upper() == "EMPTY" or PLACEHOLDER_PATTERN.search(vlm_api_key):
            failures.append("VLLM_API_KEY must be a real secret when EXTRACTION_ENABLE_VLM_FALLBACK=true")

    bind_keys = [key for key in values if key.endswith("_BIND")]
    for key in sorted(bind_keys):
        value = values[key]
        if value == "0.0.0.0" and key not in {"GATEWAY_HTTP_BIND", "GATEWAY_HTTPS_BIND"}:
            failures.append(f"{key}=0.0.0.0 exposes the service directly; put it behind a reverse proxy or private interface")

    if values.get("INGESTION_BIND", "127.0.0.1") != "127.0.0.1":
        failures.append("INGESTION_BIND should stay on 127.0.0.1 in production; expose the API through the gateway")

    if values.get("GATEWAY_HTTP_BIND", "127.0.0.1") == "0.0.0.0":
        failures.append("GATEWAY_HTTP_BIND should stay on 127.0.0.1 unless HTTP is intentionally exposed")

    max_age_days = positive_int(values, "CREDENTIAL_MAX_AGE_DAYS", failures)
    check_rotation_age(values, "INGESTION_KEYS_ROTATED_AT", max_age_days, now, failures)
    check_rotation_age(values, "DATABASE_CREDENTIALS_ROTATED_AT", max_age_days, now, failures)

    raw_retention = positive_int(values, "RAW_ARTIFACT_RETENTION_DAYS", failures)
    derived_retention = positive_int(values, "DERIVED_ARTIFACT_RETENTION_DAYS", failures)
    audit_retention = positive_int(values, "AUDIT_RETENTION_DAYS", failures)
    positive_int(values, "BACKUP_RETENTION_DAYS", failures)
    if raw_retention and derived_retention and derived_retention < raw_retention:
        failures.append("DERIVED_ARTIFACT_RETENTION_DAYS must be at least RAW_ARTIFACT_RETENTION_DAYS")
    if audit_retention and derived_retention and audit_retention < derived_retention:
        failures.append("AUDIT_RETENTION_DAYS must be at least DERIVED_ARTIFACT_RETENTION_DAYS")
    if not is_true(values.get("BACKUP_ENCRYPTION_ENABLED", "")):
        failures.append("BACKUP_ENCRYPTION_ENABLED must be true")
    if len(values.get("BACKUP_ENCRYPTION_KEY", "")) < 32:
        failures.append("BACKUP_ENCRYPTION_KEY must contain at least 32 characters")

    if is_true(values.get("OCR_FORCE_FALLBACK", "")):
        failures.append("OCR_FORCE_FALLBACK must be false")

    if environment == "production":
        if values.get("INFRASTRUCTURE_PROFILE") != "dedicated-persistent":
            failures.append("INFRASTRUCTURE_PROFILE must be dedicated-persistent in production")
        if is_true(values.get("ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK", "")):
            failures.append("ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK must be false in production")
        if is_true(values.get("REVIEW_FALLBACK_TO_LOCAL_QUEUE", "")):
            failures.append("REVIEW_FALLBACK_TO_LOCAL_QUEUE must be false in production")
        if is_true(values.get("LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN", "")):
            failures.append("LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN must be false in production")
        if values.get("LABEL_STUDIO_AUTH_SCHEME", "").lower() not in {"pat", "bearer"}:
            failures.append("LABEL_STUDIO_AUTH_SCHEME must be pat or bearer in production")
        if values.get("OCR_BACKEND", "").lower() != "tesseract":
            failures.append("OCR_BACKEND must match the currently published and benchmarked tesseract release profile")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate production IDP environment settings.")
    parser.add_argument("--env-file", default=".env.production", help="Path to the production env file")
    parser.add_argument("--environment", choices=("production", "staging"), default="production")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"preflight failed: {env_path} does not exist", file=sys.stderr)
        return 2

    try:
        values = parse_env(env_path)
    except ValueError as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 2

    failures = check_env(values, environment=args.environment)
    if failures:
        print("production preflight failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"production preflight passed for {env_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
