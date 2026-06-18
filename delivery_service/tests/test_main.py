from __future__ import annotations

import asyncio
import hmac
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from delivery_service.app.main import (
    _delivery_id,
    _provider_names,
    _redact_payload,
    _stable_json,
    _webhook_envelope,
    _webhook_event_id,
    _webhook_headers,
    app,
    settings,
)
from delivery_service.app.models import DeliveryContext, DeliveryRequest, WebhookEventRequest
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

    def test_webhook_event_id_uses_idempotency_key(self) -> None:
        first = WebhookEventRequest(
            event_type="document.completed",
            tenant_id="tenant-a",
            job_id="job-1",
            idempotency_key="tenant-a:job-1:completed",
            data={"status": "delivered"},
        )
        second = WebhookEventRequest(
            event_type="document.completed",
            tenant_id="tenant-a",
            job_id="job-1",
            idempotency_key="tenant-a:job-1:completed",
            data={"status": "changed"},
        )

        self.assertEqual(_webhook_event_id(first), _webhook_event_id(second))
        self.assertTrue(_webhook_event_id(first).startswith("evt-"))

    def test_webhook_envelope_and_signature_headers(self) -> None:
        request = WebhookEventRequest(
            event_type="document.completed",
            tenant_id="tenant-a",
            job_id="job-1",
            workflow_id="workflow-1",
            occurred_at="2026-06-15T00:00:00+00:00",
            data={"status": "delivered"},
        )
        envelope = _webhook_envelope(request, "evt-1")
        body = _stable_json(envelope).encode("utf-8")

        with patch.object(settings, "delivery_webhook_secret", "webhook-secret"):
            headers = _webhook_headers("evt-1", envelope, body)

        expected_signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
        self.assertEqual(envelope["schema_version"], "1.0")
        self.assertEqual(envelope["tenant_id"], "tenant-a")
        self.assertEqual(headers["Idempotency-Key"], "evt-1")
        self.assertEqual(headers["X-IDP-Event-Type"], "document.completed")
        self.assertEqual(headers["X-IDP-Signature-256"], f"sha256={expected_signature}")

    @patch("delivery_service.app.main.upload_json")
    @patch("delivery_service.app.main._persist_webhook_receipt")
    @patch("delivery_service.app.main._load_webhook_receipt", return_value=None)
    def test_webhook_event_without_destination_is_skipped(
        self,
        mocked_load_receipt,
        mocked_persist_receipt,
        mocked_upload_json,
    ) -> None:
        mocked_persist_receipt.return_value = (
            "delivery-artifacts",
            "jobs/job-1/webhooks/evt-1/receipt.json",
        )
        client = TestClient(app)

        with patch.object(settings, "delivery_webhook_url", ""):
            response = client.post(
                "/webhooks/events",
                json={
                    "event_id": "evt-1",
                    "event_type": "document.completed",
                    "tenant_id": "tenant-a",
                    "job_id": "job-1",
                    "data": {"status": "delivered"},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["webhook_status"], "skipped")
        self.assertFalse(payload["idempotent_replay"])
        mocked_persist_receipt.assert_called_once()
        mocked_upload_json.assert_called_once()

    @patch("delivery_service.app.main.upload_json")
    @patch("delivery_service.app.main._persist_webhook_receipt")
    @patch("delivery_service.app.main._send_webhook_event", new_callable=AsyncMock)
    @patch("delivery_service.app.main._load_webhook_receipt", return_value=None)
    def test_webhook_event_delivery_success(
        self,
        mocked_load_receipt,
        mocked_send_webhook_event,
        mocked_persist_receipt,
        mocked_upload_json,
    ) -> None:
        mocked_send_webhook_event.return_value = {
            "status": "success",
            "status_code": 202,
            "attempts": [{"attempt": 1, "status_code": 202, "success": True}],
        }
        mocked_persist_receipt.return_value = (
            "delivery-artifacts",
            "jobs/job-1/webhooks/evt-1/receipt.json",
        )
        client = TestClient(app)

        with (
            patch.object(settings, "delivery_webhook_url", "https://consumer.example.com/idp/webhooks"),
            patch.object(settings, "delivery_webhook_secret", "webhook-secret"),
        ):
            response = client.post(
                "/webhooks/events",
                json={
                    "event_id": "evt-1",
                    "event_type": "document.completed",
                    "tenant_id": "tenant-a",
                    "job_id": "job-1",
                    "workflow_id": "workflow-1",
                    "data": {"status": "delivered"},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["webhook_status"], "success")
        mocked_send_webhook_event.assert_awaited_once()
        mocked_persist_receipt.assert_called_once()
        mocked_upload_json.assert_called_once()

    @patch("delivery_service.app.main._load_webhook_receipt")
    def test_webhook_success_receipt_returns_idempotent_replay(self, mocked_load_receipt) -> None:
        mocked_load_receipt.return_value = {
            "event_id": "evt-1",
            "event_type": "document.completed",
            "job_id": "job-1",
            "tenant_id": "tenant-a",
            "webhook_status": "success",
            "webhook_receipt_key": "jobs/job-1/webhooks/evt-1/receipt.json",
        }
        client = TestClient(app)
        response = client.post(
            "/webhooks/events",
            json={
                "event_id": "evt-1",
                "event_type": "document.completed",
                "tenant_id": "tenant-a",
                "job_id": "job-1",
                "data": {"status": "delivered"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
