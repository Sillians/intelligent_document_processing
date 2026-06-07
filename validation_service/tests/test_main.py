from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from validation_service.app.main import app, infer_route, validate_payload


class ValidationServiceTests(unittest.TestCase):
    def test_invoice_auto_approved_when_required_fields_and_confidence_pass(self) -> None:
        result = validate_payload(
            job_id="job-1",
            extraction_key="jobs/job-1/extraction/result.json",
            route="invoice",
            confidence=0.94,
            used_vlm_fallback=False,
            fields={
                "invoice_number": "INV-001",
                "invoice_date": "2026-06-01",
                "total_amount": "$500.00",
                "currency": "USD",
                "vendor_name": "ACME Ltd",
            },
            field_confidences={"invoice_number": 0.95, "invoice_date": 0.92, "total_amount": 0.94},
        )

        self.assertEqual(result["verdict"], "auto_approved")
        self.assertFalse(result["requires_human_review"])
        self.assertEqual(result["reasons"], [])

    def test_missing_required_fields_route_to_review(self) -> None:
        result = validate_payload(
            job_id="job-1",
            extraction_key="jobs/job-1/extraction/result.json",
            route="invoice",
            confidence=0.95,
            used_vlm_fallback=False,
            fields={"invoice_number": "INV-001"},
        )

        self.assertEqual(result["verdict"], "needs_review")
        self.assertTrue(result["requires_human_review"])
        self.assertIn("missing_required_fields:invoice_date,total_amount", result["reasons"])

    def test_invalid_money_and_low_confidence_are_reasons(self) -> None:
        result = validate_payload(
            job_id="job-1",
            extraction_key="jobs/job-1/extraction/result.json",
            route="receipt",
            confidence=0.72,
            used_vlm_fallback=False,
            fields={
                "receipt_date": "2026-06-01",
                "merchant_name": "Corner Shop",
                "total_amount": "five hundred",
            },
        )

        self.assertEqual(result["verdict"], "needs_review")
        self.assertIn("invalid_money_format:total_amount", result["reasons"])
        self.assertIn("low_confidence:0.720", result["reasons"])

    def test_unusable_extraction_is_rejected(self) -> None:
        result = validate_payload(
            job_id="job-1",
            extraction_key="jobs/job-1/extraction/result.json",
            route="invoice",
            confidence=0.1,
            used_vlm_fallback=False,
            fields={},
        )

        self.assertEqual(result["verdict"], "rejected")
        self.assertTrue(result["requires_human_review"])
        self.assertIn("reject_unusable_extraction", result["reasons"])

    def test_infers_route_from_field_names(self) -> None:
        self.assertEqual(infer_route({"purchase_order_number": "PO-100", "supplier_name": "ACME"}), "purchase_order")
        self.assertEqual(infer_route({"account_number": "1234", "closing_balance": "$10.00"}), "bank_statement")

    @patch("validation_service.app.main.upload_json")
    def test_validate_endpoint_persists_decision(self, mocked_upload_json) -> None:
        client = TestClient(app)
        response = client.post(
            "/validate",
            json={
                "job_id": "job-1",
                "extraction_key": "jobs/job-1/extraction/result.json",
                "fields": {
                    "invoice_number": "INV-001",
                    "invoice_date": "2026-06-01",
                    "total_amount": "$500.00",
                },
                "confidence": 0.95,
                "used_vlm_fallback": False,
                "route": "invoice",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["verdict"], "auto_approved")
        self.assertEqual(payload["validation_key"], "jobs/job-1/validation/result.json")
        mocked_upload_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
