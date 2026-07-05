import unittest
from datetime import UTC, datetime

from scripts.production_preflight import check_env


class ProductionPreflightTests(unittest.TestCase):
    def test_accepts_minimal_hardened_environment(self) -> None:
        values = {
            "ENVIRONMENT": "production",
            "POSTGRES_PASSWORD": "p" * 32,
            "PUBLIC_BASE_URL": "https://idp.company.test",
            "S3_ACCESS_KEY": "a" * 32,
            "S3_SECRET_KEY": "s" * 32,
            "AWS_ACCESS_KEY_ID": "a" * 32,
            "AWS_SECRET_ACCESS_KEY": "s" * 32,
            "INGESTION_REQUIRE_AUTH": "true",
            "INGESTION_API_KEYS": f"{'k' * 32}:default",
            "LABEL_STUDIO_PASSWORD": "l" * 32,
            "GRAFANA_ADMIN_PASSWORD": "g" * 32,
            "GATEWAY_HTTPS_BIND": "0.0.0.0",
            "GATEWAY_HTTPS_HOST_PORT": "443",
            "GATEWAY_HTTP_BIND": "127.0.0.1",
            "DELIVERY_WEBHOOK_REQUIRE_SIGNATURE": "true",
            "DELIVERY_ALLOW_REQUEST_WEBHOOK_URL": "false",
            "INGESTION_BIND": "127.0.0.1",
            "INGESTION_KEYS_ROTATED_AT": "2026-07-01T00:00:00Z",
            "DATABASE_CREDENTIALS_ROTATED_AT": "2026-07-01T00:00:00Z",
            "CREDENTIAL_MAX_AGE_DAYS": "90",
            "RAW_ARTIFACT_RETENTION_DAYS": "30",
            "DERIVED_ARTIFACT_RETENTION_DAYS": "90",
            "AUDIT_RETENTION_DAYS": "365",
            "BACKUP_RETENTION_DAYS": "30",
            "BACKUP_ENCRYPTION_ENABLED": "true",
            "BACKUP_ENCRYPTION_KEY": "b" * 32,
            "INFRASTRUCTURE_PROFILE": "dedicated-persistent",
            "OCR_FORCE_FALLBACK": "false",
            "OCR_BACKEND": "tesseract",
            "ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK": "false",
            "REVIEW_FALLBACK_TO_LOCAL_QUEUE": "false",
            "LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN": "false",
            "LABEL_STUDIO_AUTH_SCHEME": "pat",
        }

        self.assertEqual(
            check_env(values, now=datetime(2026, 7, 3, tzinfo=UTC)),
            [],
        )

    def test_rejects_development_defaults_and_public_binds(self) -> None:
        values = {
            "ENVIRONMENT": "production",
            "POSTGRES_PASSWORD": "idp_password",
            "PUBLIC_BASE_URL": "https://idp.example.com",
            "S3_ACCESS_KEY": "idpadmin",
            "S3_SECRET_KEY": "idpsecret123",
            "INGESTION_REQUIRE_AUTH": "false",
            "INGESTION_API_KEYS": "dev-ingestion-key:default",
            "LABEL_STUDIO_PASSWORD": "replace-with-a-strong-password",
            "GRAFANA_ADMIN_USER": "admin",
            "GRAFANA_ADMIN_PASSWORD": "admin",
            "GATEWAY_HTTP_BIND": "0.0.0.0",
            "GATEWAY_HTTPS_HOST_PORT": "443",
            "DELIVERY_WEBHOOK_REQUIRE_SIGNATURE": "false",
            "DELIVERY_ALLOW_REQUEST_WEBHOOK_URL": "true",
            "INGESTION_BIND": "0.0.0.0",
            "INGESTION_KEYS_ROTATED_AT": "2025-01-01",
            "DATABASE_CREDENTIALS_ROTATED_AT": "2025-01-01",
            "CREDENTIAL_MAX_AGE_DAYS": "90",
            "RAW_ARTIFACT_RETENTION_DAYS": "90",
            "DERIVED_ARTIFACT_RETENTION_DAYS": "30",
            "AUDIT_RETENTION_DAYS": "7",
            "BACKUP_RETENTION_DAYS": "0",
            "BACKUP_ENCRYPTION_ENABLED": "false",
            "BACKUP_ENCRYPTION_KEY": "short",
            "INFRASTRUCTURE_PROFILE": "local",
            "OCR_FORCE_FALLBACK": "true",
            "OCR_BACKEND": "paddle",
            "ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK": "true",
            "REVIEW_FALLBACK_TO_LOCAL_QUEUE": "true",
            "LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN": "true",
            "LABEL_STUDIO_AUTH_SCHEME": "token",
        }

        failures = check_env(values, now=datetime(2026, 7, 3, tzinfo=UTC))

        self.assertTrue(any("INGESTION_REQUIRE_AUTH" in failure for failure in failures))
        self.assertTrue(any("POSTGRES_PASSWORD" in failure for failure in failures))
        self.assertTrue(any("INGESTION_BIND=0.0.0.0" in failure for failure in failures))
        self.assertTrue(any("GATEWAY_HTTP_BIND" in failure for failure in failures))
        self.assertTrue(any("INGESTION_KEYS_ROTATED_AT is older" in failure for failure in failures))
        self.assertTrue(any("BACKUP_ENCRYPTION_ENABLED" in failure for failure in failures))
        self.assertTrue(any("ORCHESTRATOR_ENABLE_OCR_NETWORK_FALLBACK" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
