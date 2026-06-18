import unittest

from scripts.production_preflight import check_env


class ProductionPreflightTests(unittest.TestCase):
    def test_accepts_minimal_hardened_environment(self) -> None:
        values = {
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
        }

        self.assertEqual(check_env(values), [])

    def test_rejects_development_defaults_and_public_binds(self) -> None:
        values = {
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
        }

        failures = check_env(values)

        self.assertTrue(any("INGESTION_REQUIRE_AUTH" in failure for failure in failures))
        self.assertTrue(any("POSTGRES_PASSWORD" in failure for failure in failures))
        self.assertTrue(any("INGESTION_BIND=0.0.0.0" in failure for failure in failures))
        self.assertTrue(any("GATEWAY_HTTP_BIND" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
