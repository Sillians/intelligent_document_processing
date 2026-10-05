from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.full_pipeline_e2e import poll_until_complete, validate_pipeline_result


def base_result(final_status: str = "delivered") -> dict:
    result = {
        "job_id": "job-1",
        "status": final_status,
        "preprocess": {"preprocessed_key": "jobs/job-1/preprocess/document.png"},
        "ocr": {"ocr_key": "jobs/job-1/ocr/ocr.json", "mean_confidence": 0.91},
        "layout": {"layout_key": "jobs/job-1/layout/layout.json", "block_count": 4},
        "classification": {"route": "invoice"},
        "extraction": {
            "extraction_key": "jobs/job-1/extraction/result.json",
            "route": "invoice",
            "fields": {
                "invoice_number": "INV-001",
                "invoice_date": "2026/06/08",
                "total_amount": "$120.00",
            },
            "confidence": 0.89,
            "used_vlm_fallback": False,
        },
        "validation": {
            "verdict": "auto_approved",
            "requires_human_review": False,
            "route": "invoice",
        },
    }
    if final_status == "delivered":
        result["delivery"] = {
            "delivery_status": "success",
            "delivery_id": "delivery-1",
            "delivery_receipt_key": "jobs/job-1/delivery/delivery-1/receipt.json",
        }
    else:
        result["review_task"] = {
            "review_status": "queued_without_label_studio",
            "review_task_id": "review-1",
        }
        result["validation"]["verdict"] = "needs_review"
        result["validation"]["requires_human_review"] = True
    return {"job_id": "job-1", "result": result}


class FullPipelineE2EValidationTests(unittest.TestCase):
    @patch("scripts.full_pipeline_e2e.get_status", return_value={"workflow_status": "PENDING_HUMAN_REVIEW"})
    def test_smoke_returns_pending_review_snapshot_without_waiting_for_final_delivery(self, _get_status) -> None:
        status, history = poll_until_complete(SimpleNamespace(timeout_seconds=2, poll_interval_seconds=0), "job-1")
        self.assertEqual(status["workflow_status"], "PENDING_HUMAN_REVIEW")
        self.assertEqual(len(history), 1)

    def test_delivered_result_passes_contract(self) -> None:
        summary, errors = validate_pipeline_result(base_result(), strict_required_fields=False)

        self.assertEqual(errors, [])
        self.assertEqual(summary["workflow_final_status"], "delivered")
        self.assertEqual(summary["branch"], "delivery")

    def test_page_count_mismatch_fails_contract(self) -> None:
        payload = base_result()
        payload["result"]["preprocess"]["page_count"] = 2
        payload["result"]["ocr"]["page_count"] = 1
        payload["result"]["layout"]["page_count"] = 2
        _, errors = validate_pipeline_result(payload, strict_required_fields=False)
        self.assertIn("page_count_mismatch", errors)

    def test_pending_review_result_passes_contract_with_missing_invoice_fields(self) -> None:
        payload = base_result(final_status="pending_human_review")
        payload["result"]["extraction"]["fields"]["total_amount"] = ""

        summary, errors = validate_pipeline_result(payload, strict_required_fields=False)

        self.assertEqual(errors, [])
        self.assertEqual(summary["workflow_final_status"], "pending_human_review")
        self.assertEqual(summary["branch"], "review")
        self.assertEqual(summary["missing_required_fields"], ["total_amount"])

    def test_rejected_or_timed_out_review_does_not_pass_smoke(self) -> None:
        for status in ("rejected", "review_validation_failed", "review_timed_out"):
            with self.subTest(status=status):
                _, errors = validate_pipeline_result(base_result(final_status=status), strict_required_fields=False)
                self.assertIn(f"unexpected_final_status:{status}", errors)

    def test_strict_required_fields_fails_missing_fields_even_on_review_branch(self) -> None:
        payload = base_result(final_status="pending_human_review")
        payload["result"]["extraction"]["fields"]["invoice_date"] = ""

        _, errors = validate_pipeline_result(payload, strict_required_fields=True)

        self.assertIn("missing_required_fields:invoice_date", errors)

    def test_delivered_result_fails_when_delivery_receipt_missing(self) -> None:
        payload = base_result()
        payload["result"]["delivery"]["delivery_receipt_key"] = ""

        _, errors = validate_pipeline_result(payload, strict_required_fields=False)

        self.assertIn("delivery:missing_delivery_receipt_key", errors)

    def test_missing_stage_fails_contract(self) -> None:
        payload = base_result()
        del payload["result"]["layout"]

        _, errors = validate_pipeline_result(payload, strict_required_fields=False)

        self.assertIn("missing_or_invalid_stage:layout", errors)


if __name__ == "__main__":
    unittest.main()
