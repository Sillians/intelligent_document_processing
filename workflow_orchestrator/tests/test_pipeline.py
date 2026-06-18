from __future__ import annotations

import unittest

from workflow_orchestrator.app.pipeline import (
    PipelineContractError,
    build_classification_payload,
    build_delivery_payload,
    build_evaluation_payload,
    build_extraction_payload,
    build_layout_payload,
    build_ocr_payload,
    build_preprocess_payload,
    build_review_payload,
    build_validation_payload,
    build_webhook_event_payload,
    should_route_to_human_review,
)


class PipelineContractTests(unittest.TestCase):
    def test_build_preprocess_payload_success(self) -> None:
        payload = {
            "job_id": "job-1",
            "raw_bucket": "raw-documents",
            "raw_key": "jobs/job-1/raw/doc.pdf",
        }

        out = build_preprocess_payload(payload)
        self.assertEqual(out["job_id"], "job-1")
        self.assertEqual(out["raw_bucket"], "raw-documents")
        self.assertEqual(out["raw_key"], "jobs/job-1/raw/doc.pdf")

    def test_build_preprocess_payload_missing_field_raises(self) -> None:
        with self.assertRaises(PipelineContractError):
            build_preprocess_payload({"job_id": "job-1", "raw_bucket": "raw"})

    def test_stage_payload_chain_success(self) -> None:
        preprocess_result = {"preprocessed_key": "jobs/job-1/preprocessed/page-0001.png"}
        ocr_result = {"ocr_key": "jobs/job-1/ocr/ocr.json", "mean_confidence": 0.91}
        layout_result = {"layout_key": "jobs/job-1/layout/layout.json"}
        classification_result = {"route": "invoice"}
        extraction_result = {
            "extraction_bucket": "extraction-artifacts",
            "extraction_key": "jobs/job-1/extraction/result.json",
            "fields": {"invoice_number": "INV-001"},
            "confidence": 0.93,
            "used_vlm_fallback": False,
        }
        validation_result = {
            "requires_human_review": False,
            "reasons": [],
            "verdict": "auto_approved",
            "validation_bucket": "validation-artifacts",
            "validation_key": "jobs/job-1/validation/result.json",
        }

        self.assertEqual(
            build_ocr_payload("job-1", preprocess_result),
            {"job_id": "job-1", "preprocessed_key": "jobs/job-1/preprocessed/page-0001.png"},
        )
        self.assertEqual(
            build_layout_payload("job-1", preprocess_result, ocr_result),
            {
                "job_id": "job-1",
                "preprocessed_key": "jobs/job-1/preprocessed/page-0001.png",
                "ocr_key": "jobs/job-1/ocr/ocr.json",
            },
        )
        self.assertEqual(
            build_classification_payload("job-1", ocr_result),
            {"job_id": "job-1", "ocr_key": "jobs/job-1/ocr/ocr.json"},
        )

        extract_payload = build_extraction_payload("job-1", ocr_result, layout_result, classification_result)
        self.assertEqual(extract_payload["route"], "invoice")
        self.assertAlmostEqual(extract_payload["ocr_confidence"], 0.91, places=4)

        validate_payload = build_validation_payload("job-1", extraction_result)
        self.assertEqual(validate_payload["job_id"], "job-1")
        self.assertEqual(validate_payload["extraction_bucket"], "extraction-artifacts")
        self.assertFalse(validate_payload["used_vlm_fallback"])

        self.assertFalse(should_route_to_human_review(validation_result))

        delivery_payload = build_delivery_payload("job-1", extraction_result, validation_result)
        self.assertEqual(delivery_payload["payload"], {"invoice_number": "INV-001"})
        self.assertEqual(delivery_payload["approval_status"], "auto_approved")
        self.assertEqual(delivery_payload["validation_key"], "jobs/job-1/validation/result.json")

        review_payload = build_review_payload(
            "job-1",
            extraction_result,
            {
                "requires_human_review": True,
                "reasons": ["low_confidence"],
                "validation_bucket": "validation-artifacts",
                "validation_key": "jobs/job-1/validation/result.json",
                "verdict": "needs_review",
            },
        )
        self.assertEqual(review_payload["reasons"], ["low_confidence"])
        self.assertEqual(review_payload["extraction_key"], "jobs/job-1/extraction/result.json")
        self.assertEqual(review_payload["validation_key"], "jobs/job-1/validation/result.json")
        self.assertEqual(review_payload["verdict"], "needs_review")

    def test_build_validation_payload_contract_error(self) -> None:
        with self.assertRaises(PipelineContractError):
            build_validation_payload(
                "job-1",
                {
                    "extraction_key": "jobs/job-1/extraction/result.json",
                    "confidence": 0.5,
                    "used_vlm_fallback": False,
                },
            )

    def test_should_route_to_human_review_parses_string_booleans(self) -> None:
        self.assertTrue(should_route_to_human_review({"requires_human_review": "true"}))
        self.assertFalse(should_route_to_human_review({"requires_human_review": "0"}))

    def test_build_evaluation_payload_defaults_on_invalid_inputs(self) -> None:
        payload = build_evaluation_payload(
            job_id="job-1",
            final_status="failed",
            ocr_result={"mean_confidence": "bad"},
            extraction_result={"confidence": None, "used_vlm_fallback": "bad"},
            validation_result={"requires_human_review": "bad"},
        )

        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["ocr_confidence"], 0.0)
        self.assertEqual(payload["extraction_confidence"], 0.0)
        self.assertFalse(payload["used_vlm_fallback"])
        self.assertFalse(payload["requires_human_review"])

    def test_build_evaluation_payload_includes_quality_context(self) -> None:
        payload = build_evaluation_payload(
            job_id="job-1",
            final_status="delivered",
            ocr_result={"mean_confidence": 0.91},
            extraction_result={
                "confidence": 0.93,
                "used_vlm_fallback": False,
                "fields": {"invoice_number": "INV-001", "currency": ""},
            },
            validation_result={
                "requires_human_review": False,
                "route": "invoice",
                "verdict": "auto_approved",
            },
        )

        self.assertEqual(payload["route"], "invoice")
        self.assertEqual(payload["validation_verdict"], "auto_approved")
        self.assertEqual(payload["field_count"], 2)
        self.assertEqual(payload["populated_field_count"], 1)

    def test_build_webhook_event_payload_for_completed_document(self) -> None:
        payload = build_webhook_event_payload(
            event_type="document.completed",
            tenant_id="tenant-a",
            job_id="job-1",
            workflow_id="workflow-1",
            final_status="delivered",
            result={
                "classification": {"route": "invoice"},
                "validation": {"verdict": "auto_approved"},
                "delivery": {"delivery_status": "success"},
            },
        )

        self.assertEqual(payload["event_type"], "document.completed")
        self.assertEqual(payload["tenant_id"], "tenant-a")
        self.assertEqual(payload["idempotency_key"], "document.completed:tenant-a:job-1:workflow-1")
        self.assertEqual(payload["data"]["status"], "delivered")
        self.assertEqual(payload["data"]["classification"], {"route": "invoice"})
        self.assertEqual(payload["data"]["delivery"], {"delivery_status": "success"})

    def test_build_webhook_event_payload_defaults_tenant_and_truncates_error(self) -> None:
        payload = build_webhook_event_payload(
            event_type="document.failed",
            tenant_id="",
            job_id="job-1",
            workflow_id=None,
            final_status="failed",
            error="x" * 700,
        )

        self.assertEqual(payload["tenant_id"], "default")
        self.assertEqual(payload["idempotency_key"], "document.failed:default:job-1:")
        self.assertEqual(len(payload["data"]["error"]), 500)


if __name__ == "__main__":
    unittest.main()
