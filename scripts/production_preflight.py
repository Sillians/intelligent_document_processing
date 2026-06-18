#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


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
}
REQUIRED_KEYS = {
    "PUBLIC_BASE_URL",
    "POSTGRES_PASSWORD",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "GATEWAY_HTTPS_HOST_PORT",
    "INGESTION_REQUIRE_AUTH",
    "INGESTION_API_KEYS",
    "LABEL_STUDIO_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
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


def check_env(values: dict[str, str]) -> list[str]:
    failures: list[str] = []

    for key in sorted(REQUIRED_KEYS):
        if not values.get(key):
            failures.append(f"{key} is required")

    public_base_url = values.get("PUBLIC_BASE_URL", "")
    if public_base_url and PLACEHOLDER_PATTERN.search(public_base_url):
        failures.append("PUBLIC_BASE_URL must be set to the real production URL")

    if is_false(values.get("INGESTION_REQUIRE_AUTH", "")):
        failures.append("INGESTION_REQUIRE_AUTH must be true in production")

    api_keys = values.get("INGESTION_API_KEYS", "")
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

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate production IDP environment settings.")
    parser.add_argument("--env-file", default=".env.production", help="Path to the production env file")
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

    failures = check_env(values)
    if failures:
        print("production preflight failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"production preflight passed for {env_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
