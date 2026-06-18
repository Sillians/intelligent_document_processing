from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from human_review_console.app.main import (
    LabelStudioProvider,
    ProviderResult,
    ReviewTaskRequest,
    app,
    build_review_payload,
    build_review_task_id,
    derive_priority,
    extract_label_studio_task_ref,
    settings,
)


class HumanReviewConsoleTests(unittest.TestCase):
    def test_review_task_id_is_stable_for_retries(self) -> None:
        task = ReviewTaskRequest(
            job_id="job-1",
            reasons=["low_confidence:0.720"],
            fields={"invoice_number": "INV-001"},
            confidence=0.72,
        )

        self.assertEqual(build_review_task_id(task), build_review_task_id(task))
        self.assertTrue(build_review_task_id(task).startswith("review-"))

    def test_priority_derivation(self) -> None:
        high = ReviewTaskRequest(job_id="job-1", reasons=[], fields={}, confidence=0.2)
        medium = ReviewTaskRequest(job_id="job-2", reasons=["low_confidence:0.700"], fields={}, confidence=0.7)
        normal = ReviewTaskRequest(job_id="job-3", reasons=["missing_recommended_fields:x"], fields={}, confidence=0.95)

        self.assertEqual(derive_priority(high), "high")
        self.assertEqual(derive_priority(medium), "medium")
        self.assertEqual(derive_priority(normal), "normal")

    def test_build_review_payload_contains_sources(self) -> None:
        task = ReviewTaskRequest(
            job_id="job-1",
            reasons=["missing_required_fields:total_amount"],
            fields={"invoice_number": "INV-001"},
            confidence=0.88,
            route="invoice",
            verdict="needs_review",
            extraction_bucket="extraction-artifacts",
            extraction_key="jobs/job-1/extraction/result.json",
            validation_bucket="validation-artifacts",
            validation_key="jobs/job-1/validation/result.json",
        )

        payload = build_review_payload(task, review_task_id="review-abc")

        self.assertEqual(payload["review_task_id"], "review-abc")
        self.assertEqual(payload["route"], "invoice")
        self.assertEqual(payload["source"]["validation_key"], "jobs/job-1/validation/result.json")

    def test_extract_label_studio_task_ref_handles_common_shapes(self) -> None:
        self.assertEqual(extract_label_studio_task_ref({"task_ids": [123]}, default="fallback"), "123")
        self.assertEqual(extract_label_studio_task_ref({"id": 456}, default="fallback"), "456")
        self.assertEqual(extract_label_studio_task_ref([{"id": 789}], default="fallback"), "789")
        self.assertEqual(extract_label_studio_task_ref({}, default="fallback"), "fallback")

    @patch("human_review_console.app.main.upload_json")
    def test_create_review_task_uses_local_queue_without_label_studio_token(self, mocked_upload_json) -> None:
        client = TestClient(app)
        response = client.post(
            "/review/tasks",
            json={
                "job_id": "job-1",
                "reasons": ["low_confidence:0.720"],
                "fields": {"invoice_number": "INV-001"},
                "confidence": 0.72,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["review_status"], "queued_without_label_studio")
        self.assertEqual(payload["provider"], "local_queue")
        self.assertEqual(payload["review_task_key"], f"jobs/job-1/review/{payload['review_task_id']}.json")
        mocked_upload_json.assert_called_once()

    @patch("human_review_console.app.main.select_provider")
    @patch("human_review_console.app.main.upload_json")
    def test_create_review_task_falls_back_to_local_queue_on_provider_failure(self, mocked_upload_json, mocked_select_provider) -> None:
        class BrokenProvider:
            name = "broken"

            async def create_task(self, task, *, review_task_id, payload):
                raise RuntimeError("provider unavailable")

        mocked_select_provider.return_value = BrokenProvider()
        client = TestClient(app)
        response = client.post(
            "/review/tasks",
            json={
                "job_id": "job-2",
                "reasons": ["reject_unusable_extraction"],
                "fields": {},
                "confidence": 0.1,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["review_status"], "queued_without_label_studio")
        self.assertEqual(payload["provider"], "local_queue")
        self.assertEqual(payload["priority"], "high")
        mocked_upload_json.assert_called_once()

    def test_label_studio_provider_requires_token(self) -> None:
        provider = LabelStudioProvider()
        task = ReviewTaskRequest(job_id="job-1", reasons=[], fields={}, confidence=0.9)
        with patch.object(settings, "label_studio_token", ""), self.assertRaises(RuntimeError):
            import asyncio

            asyncio.run(provider.create_task(task, review_task_id="review-1", payload={"route": "unknown", "verdict": "needs_review", "priority": "normal", "source": {}}))


if __name__ == "__main__":
    unittest.main()
