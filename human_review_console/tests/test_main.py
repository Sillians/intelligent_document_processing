from __future__ import annotations

import unittest
import httpx
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi import HTTPException

from human_review_console.app.main import (
    LabelStudioProvider,
    ProviderResult,
    ReviewTaskRequest,
    app,
    build_review_payload,
    build_review_task_id,
    derive_priority,
    extract_label_studio_task_ref,
    label_studio_auth_mode,
    _fields_from_label_studio,
    settings,
)


class HumanReviewConsoleTests(unittest.TestCase):
    def test_webhook_requires_tenant_key_and_uses_authoritative_lookup(self):
        with patch.object(settings, "review_decision_api_keys", "secret:tenant-a"), patch(
            "human_review_console.app.main.poll_review_decision", new_callable=AsyncMock,
            return_value={"status": "pending"},
        ) as lookup:
            client = TestClient(app)
            path = "/review/tasks/job-1/review-1/label-studio-event"
            self.assertEqual(client.post(path + "?tenant_id=tenant-b", headers={"X-IDP-Review-Key": "secret"}).status_code, 401)
            lookup.assert_not_called()
            response = client.post(path + "?tenant_id=tenant-a", headers={"X-IDP-Review-Key": "secret"}, json={"decision": "approved"})
            self.assertEqual(response.json(), {"status": "pending"})
            lookup.assert_awaited_once()

    def test_missing_reviewer_is_not_accepted(self):
        result = [{"from_name": "decision", "value": {"choices": ["Reject"]}}]
        self.assertIsNone(_fields_from_label_studio({"annotations": [{"result": result}]}))
        self.assertEqual(_fields_from_label_studio({"annotations": [{"result": result, "completed_by": {"id": 42}}]})[2], "42")
    @patch("human_review_console.app.main._save_decision")
    @patch("human_review_console.app.main._load_decision", return_value=None)
    @patch("human_review_console.app.main._checked_review", return_value={"provider": "label_studio", "external_task_id": "42"})
    def test_label_studio_poll_records_approved_annotation(self, _checked, _loaded, saved) -> None:
        task = {
            "project": 1,
            "data": {"job_id": "job-1", "tenant_id": "tenant-a", "review_task_id": "review-1"},
            "annotations": [{"completed_by": 12, "result": [
                {"from_name": "decision", "value": {"choices": ["Approve"]}},
                {"from_name": "corrected_fields_json", "value": {"text": ['{"total_amount":"42.00"}']}},
            ]}],
        }
        saved.side_effect = lambda _job, _task, decision: decision
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=task)), **kwargs)

        with patch.object(settings, "review_decision_api_key", "worker-secret"), patch.object(settings, "label_studio_token", "legacy-token"), patch.object(settings, "label_studio_auth_scheme", "token"), patch.object(settings, "label_studio_project_id", 1), patch("human_review_console.app.main.httpx.AsyncClient", side_effect=client_factory):
            response = TestClient(app).get("/review/tasks/job-1/review-1/decision?tenant_id=tenant-a", headers={"X-IDP-Review-Key": "worker-secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "decided")
        self.assertEqual(response.json()["fields"]["total_amount"], "42.00")
        saved.assert_called_once()

    def test_label_studio_annotation_extracts_reviewed_fields(self) -> None:
        parsed = _fields_from_label_studio({"annotations": [{
            "completed_by": 12,
            "result": [
                {"from_name": "decision", "value": {"choices": ["Approve"]}},
                {"from_name": "corrected_fields_json", "value": {"text": ['{"invoice_number":"INV-42","total_amount":"42.00"}']}},
            ],
        }]})
        self.assertEqual(parsed, ("approved", {"invoice_number": "INV-42", "total_amount": "42.00"}, "12"))

    def test_label_studio_annotation_without_explicit_decision_stays_pending(self) -> None:
        for result in (
            [{"from_name": "corrected_fields_json", "value": {"text": ['{"total_amount":"42.00"}']}}],
            [{"from_name": "decision", "value": {"choices": ["Maybe"]}}],
        ):
            with self.subTest(result=result):
                self.assertIsNone(_fields_from_label_studio({"annotations": [{"result": result}]}))

    @patch("human_review_console.app.main._save_decision")
    @patch("human_review_console.app.main._load_decision", return_value=None)
    @patch("human_review_console.app.main._checked_review", return_value={"provider": "local_queue"})
    def test_review_decision_requires_credential_and_records_correction(self, _checked, _loaded, saved) -> None:
        saved.side_effect = lambda _job, _task, decision: decision
        with patch.object(settings, "review_decision_api_keys", "review-secret:tenant-a"):
            client = TestClient(app)
            body = {"tenant_id": "tenant-a", "reviewer_id": "analyst-1", "decision": "approved", "fields": {"total_amount": "42.00"}}
            denied = client.post("/review/tasks/job-1/review-1/decision", json=body)
            allowed = client.post("/review/tasks/job-1/review-1/decision", json=body, headers={"X-IDP-Review-Key": "review-secret"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["tenant_id"], "tenant-a")
        saved.assert_called_once()

    @patch("human_review_console.app.main._load_review_record", return_value={"job_id": "job-1", "review_task_id": "review-1", "tenant_id": "tenant-a"})
    def test_review_record_rejects_other_tenant(self, _loaded) -> None:
        from human_review_console.app.main import _checked_review
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as raised:
            _checked_review("job-1", "review-1", "tenant-b")
        self.assertEqual(raised.exception.status_code, 404)
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

    def test_label_studio_import_without_task_id_fails_closed(self) -> None:
        task = ReviewTaskRequest(job_id="job-1", fields={}, confidence=0.5)
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})), **kwargs)

        with patch.object(settings, "label_studio_token", "legacy-token"), patch.object(settings, "label_studio_auth_scheme", "token"), patch("human_review_console.app.main.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaisesRegex(RuntimeError, "did not return a task ID"):
                import asyncio
                asyncio.run(LabelStudioProvider().create_task(task, review_task_id="review-1", payload=build_review_payload(task, review_task_id="review-1")))

    @patch("human_review_console.app.main._load_review_record", side_effect=HTTPException(404), new_callable=None)
    @patch("human_review_console.app.main.upload_json")
    def test_create_review_task_uses_local_queue_without_label_studio_token(self, mocked_upload_json, _load) -> None:
        with patch.object(settings, "label_studio_token", ""), patch.object(settings, "review_provider", "auto"):
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

    @patch("human_review_console.app.main._load_review_record", side_effect=HTTPException(404))
    @patch("human_review_console.app.main.select_provider")
    @patch("human_review_console.app.main.upload_json")
    def test_create_review_task_falls_back_to_local_queue_on_provider_failure(self, mocked_upload_json, mocked_select_provider, _load) -> None:
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

    def test_label_studio_auth_mode_supports_legacy_and_pat_tokens(self) -> None:
        with patch.object(settings, "label_studio_auth_scheme", "token"):
            self.assertEqual(label_studio_auth_mode("legacy-token"), "token")
        with patch.object(settings, "label_studio_auth_scheme", "auto"):
            self.assertEqual(label_studio_auth_mode("header.payload.signature"), "pat")

    @patch("human_review_console.app.main.LabelStudioProvider.check_connection", new_callable=AsyncMock)
    def test_provider_health_verifies_label_studio_project(self, mocked_check: AsyncMock) -> None:
        mocked_check.return_value = {
            "status": "ok",
            "provider": "label_studio",
            "project_id": 1,
            "project_title": "IntelligentDP #1",
            "auth_mode": "token",
        }
        with patch.object(settings, "label_studio_token", "configured-token"):
            response = TestClient(app).get("/health/provider")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["project_title"], "IntelligentDP #1")


if __name__ == "__main__":
    unittest.main()
