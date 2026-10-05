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
    def test_nonfinite_json_is_rejected(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as failure:
            _stable_json({'total': float('nan')})
        self.assertEqual(failure.exception.status_code, 422)

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


    def ledger_client(self):
        from tests.delivery_fake import MemoryLedger
        ledger = MemoryLedger()
        self.addCleanup(patch.stopall)
        patch("delivery_service.app.main.get_repository", return_value=ledger).start()
        patch("delivery_service.app.main._legacy_receipt", return_value=None).start()
        patch("delivery_service.app.main.upload_json", return_value="s3://receipt").start()
        patch("delivery_service.app.providers.upload_json", return_value="s3://payload").start()
        return ledger, TestClient(app)

    def test_durable_delivery_succeeds_and_replays_without_resending(self):
        ledger, client = self.ledger_client()
        body = {"job_id": "job-1", "payload": {"value": 1}, "approval_status": "auto_approved", "idempotency_key": "same"}
        response = client.post("/deliver", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["delivery_status"], "success")
        replay = client.post("/deliver", json=body)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(ledger.sent_count, 2)  # payload plus receipt; replay sends neither
        conflict = client.post("/deliver", json={**body, "payload": {"value": 2}})
        self.assertEqual(conflict.status_code, 409)

    def test_partial_failure_preserves_acknowledged_provider(self):
        ledger, client = self.ledger_client()
        from delivery_service.app.main import _process_due
        body = {"job_id": "job-2", "payload": {}, "approval_status": "auto_approved", "destinations": ["object_storage", "webhook"]}
        with patch.object(settings, "delivery_webhook_url", "https://consumer.example.com/results"), patch.object(settings, "delivery_webhook_secret", "secret"), patch("delivery_service.app.providers.WebhookProvider.deliver", new_callable=AsyncMock, side_effect=ConnectionError("offline")):
            response = client.post("/deliver", json=body)
        self.assertEqual(response.status_code, 202)
        operation = next(iter(ledger.operations.values()))
        storage = next(work for work in operation["work"] if work["provider"] == "object_storage")
        self.assertEqual(storage["state"], "acknowledged")
        self.assertEqual(storage["attempt_count"], 1)
        for work in operation["work"]:
            work["due"] = 0
        with patch("delivery_service.app.providers.WebhookProvider.deliver", new_callable=AsyncMock, return_value={"provider": "webhook", "status": "success"}):
            asyncio.run(_process_due(ledger))
            asyncio.run(_process_due(ledger))
        self.assertEqual(storage["attempt_count"], 1)
        self.assertEqual(ledger.get("default", operation["operation_id"])["state"], "success")

    def test_receipt_failure_does_not_replay_payload_provider(self):
        ledger, client = self.ledger_client()
        with patch("delivery_service.app.main._project_receipt", side_effect=ConnectionError("offline")):
            response = client.post("/deliver", json={"job_id": "job-3", "payload": {}, "approval_status": "auto_approved"})
        self.assertEqual(response.status_code, 202)
        operation = next(iter(ledger.operations.values()))
        self.assertEqual(operation["work"][0]["state"], "acknowledged")
        self.assertEqual(operation["work"][0]["attempt_count"], 1)

    def test_webhook_without_destination_is_durably_skipped(self):
        ledger, client = self.ledger_client()
        with patch.object(settings, "delivery_webhook_url", ""):
            response = client.post("/webhooks/events", json={"event_id": "event-1", "event_type": "document.completed", "job_id": "job-1", "tenant_id": "tenant-a"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["webhook_status"], "skipped")
        self.assertEqual(ledger.sent_count, 1)

    def test_storage_outage_is_not_treated_as_missing_legacy_receipt(self):
        _, client = self.ledger_client()
        with patch("delivery_service.app.main._legacy_receipt", side_effect=ConnectionError("offline")):
            response = client.post("/deliver", json={"job_id": "job-4", "payload": {}, "approval_status": "auto_approved"})
        self.assertEqual(response.status_code, 503)

    def test_legacy_receipt_blocks_unsafe_resend(self):
        _, client = self.ledger_client()
        with patch("delivery_service.app.main._legacy_receipt", return_value={"delivery_status": "success"}):
            response = client.post("/deliver", json={"job_id": "job-5", "payload": {}, "approval_status": "auto_approved"})
        self.assertEqual(response.status_code, 409)

    def test_pending_operation_cannot_be_read_as_another_tenant(self):
        _, client = self.ledger_client()
        response = client.post("/deliver", json={"job_id": "job-6", "tenant_id": "tenant-a", "payload": {}, "approval_status": "auto_approved"})
        denied = client.get("/deliveries/job-6/" + response.json()["delivery_id"] + "?tenant_id=tenant-b")
        self.assertEqual(denied.status_code, 404)

    def test_webhook_envelope_and_signature_headers(self):
        request = WebhookEventRequest(event_type="document.completed", tenant_id="tenant-a", job_id="job-1")
        envelope = _webhook_envelope(request, "event-1")
        body = _stable_json(envelope).encode()
        with patch.object(settings, "delivery_webhook_secret", "secret"):
            headers = _webhook_headers("event-1", envelope, body)
        self.assertEqual(headers["Idempotency-Key"], "event-1")
        self.assertEqual(headers["X-IDP-Signature-256"], "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest())
