from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from delivery_service.app.main import _delivery_id, _provider_names, _redact_payload, app, settings
from delivery_service.app.models import DeliveryContext, DeliveryRequest
from delivery_service.app.providers import WebhookProvider, build_provider


class DeliveryServiceTests(unittest.TestCase):
    def test_delivery_id_is_stable_for_retries(self) -> None:
        request = DeliveryRequest(
            job_id="job-1",
            payload={"invoice_number": "INV-001"},
            approval_status="auto_approved",
        )
        providers = _provider_names(request)

        self.assertEqual(_delivery_id(request, providers), _delivery_id(request, providers))
        self.assertTrue(_delivery_id(request, providers).startswith("delivery-"))

    def test_explicit_idempotency_key_controls_delivery_id(self) -> None:
        first = DeliveryRequest(job_id="job-1", payload={"value": 1}, approval_status="auto_approved", idempotency_key="same")
        second = DeliveryRequest(job_id="job-1", payload={"value": 2}, approval_status="auto_approved", idempotency_key="same")

        self.assertEqual(_delivery_id(first, ["object_storage"]), _delivery_id(second, ["webhook"]))

    def test_redacts_configured_fields_recursively(self) -> None:
        with patch.object(settings, "delivery_redact_fields", "account_number,ssn"):
            payload, redacted = _redact_payload(
                {"account_number": "1234", "nested": {"ssn": "111-22-3333", "name": "Ada"}}
            )

        self.assertEqual(payload["account_number"], "[REDACTED]")
        self.assertEqual(payload["nested"]["ssn"], "[REDACTED]")
        self.assertEqual(redacted, ["account_number", "ssn"])

    def test_provider_registry_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            build_provider("unknown")

    def test_per_request_webhook_url_is_blocked_by_default(self) -> None:
        request = DeliveryRequest(
            job_id="job-1",
            payload={"value": 1},
            approval_status="auto_approved",
            webhook_url="https://consumer.example.com/results",
        )
        context = DeliveryContext(settings=settings, request=request, delivery_id="delivery-1", envelope={})

        with self.assertRaisesRegex(ValueError, "per-request webhook_url is disabled"):
            asyncio.run(WebhookProvider().deliver(context))

    def test_webhook_signature_is_required_by_default(self) -> None:
        request = DeliveryRequest(job_id="job-1", payload={"value": 1}, approval_status="auto_approved")
        context = DeliveryContext(settings=settings, request=request, delivery_id="delivery-1", envelope={})

        with (
            patch.object(settings, "delivery_webhook_url", "https://consumer.example.com/results"),
            patch.object(settings, "delivery_webhook_secret", ""),
            self.assertRaisesRegex(ValueError, "requires DELIVERY_WEBHOOK_SECRET"),
        ):
            asyncio.run(WebhookProvider().deliver(context))

    def test_unapproved_delivery_is_rejected(self) -> None:
        client = TestClient(app)
        response = client.post("/deliver", json={"job_id": "job-1", "payload": {"value": 1}})

        self.assertEqual(response.status_code, 409)

    @patch("delivery_service.app.main.upload_json")
    @patch("delivery_service.app.providers.upload_json")
    @patch("delivery_service.app.main._persist_receipt")
    @patch("delivery_service.app.main._load_receipt", return_value=None)
    def test_object_storage_delivery_succeeds(
        self,
        mocked_load_receipt,
        mocked_persist_receipt,
        mocked_provider_upload,
        mocked_main_upload,
    ) -> None:
        mocked_provider_upload.return_value = "s3://delivery-artifacts/jobs/job-1/delivery/payload.json"
        mocked_persist_receipt.return_value = ("delivery-artifacts", "jobs/job-1/delivery/receipt.json")
        client = TestClient(app)
        response = client.post(
            "/deliver",
            json={
                "job_id": "job-1",
                "payload": {"invoice_number": "INV-001"},
                "approval_status": "auto_approved",
                "destinations": ["object_storage"],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["delivery_status"], "success")
        self.assertFalse(payload["idempotent_replay"])
        self.assertEqual(payload["destination_results"][0]["provider"], "object_storage")
        mocked_persist_receipt.assert_called_once()
        mocked_main_upload.assert_called_once()

    @patch("delivery_service.app.main._load_receipt")
    def test_successful_receipt_returns_idempotent_replay(self, mocked_load_receipt) -> None:
        mocked_load_receipt.return_value = {
            "job_id": "job-1",
            "delivery_id": "delivery-existing",
            "delivery_status": "success",
            "destination_results": [],
            "delivered_at": "2026-06-04T00:00:00+00:00",
            "delivery_bucket": "delivery-artifacts",
            "delivery_receipt_key": "jobs/job-1/delivery/delivery-existing/receipt.json",
        }
        client = TestClient(app)
        response = client.post(
            "/deliver",
            json={
                "job_id": "job-1",
                "payload": {"invoice_number": "INV-001"},
                "approval_status": "auto_approved",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
