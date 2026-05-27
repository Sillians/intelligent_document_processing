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
            "extraction_key": "jobs/job-1/extraction/result.json",
            "fields": {"invoice_number": "INV-001"},
            "confidence": 0.93,
            "used_vlm_fallback": False,
        }
        validation_result = {"requires_human_review": False, "reasons": []}

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
        self.assertFalse(validate_payload["used_vlm_fallback"])

        self.assertFalse(should_route_to_human_review(validation_result))

        delivery_payload = build_delivery_payload("job-1", extraction_result)
        self.assertEqual(delivery_payload["payload"], {"invoice_number": "INV-001"})

        review_payload = build_review_payload(
            "job-1",
            extraction_result,
            {"requires_human_review": True, "reasons": ["low_confidence"]},
        )
        self.assertEqual(review_payload["reasons"], ["low_confidence"])

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


if __name__ == "__main__":
    unittest.main()
