from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from evaluation_service.app.main import (
    _evaluation_id,
    app,
    build_metrics,
    build_parameters,
    build_tags,
    settings,
)
from evaluation_service.app.models import EvaluationRequest
from evaluation_service.app.providers import build_provider


class EvaluationServiceTests(unittest.TestCase):
    def _request(self, **updates) -> EvaluationRequest:
        payload = {
            "job_id": "job-1",
            "status": "delivered",
            "ocr_confidence": 0.9,
            "extraction_confidence": 0.8,
            "used_vlm_fallback": False,
            "requires_human_review": False,
            "route": "invoice",
            "validation_verdict": "auto_approved",
            "field_count": 4,
            "populated_field_count": 3,
        }
        payload.update(updates)
        return EvaluationRequest(**payload)

    def test_evaluation_id_is_stable_for_retries(self) -> None:
        request = self._request()
        self.assertEqual(_evaluation_id(request), _evaluation_id(request))
        self.assertTrue(_evaluation_id(request).startswith("evaluation-"))

    def test_explicit_idempotency_key_controls_evaluation_id(self) -> None:
        first = self._request(idempotency_key="same", ocr_confidence=0.2)
        second = self._request(idempotency_key="same", ocr_confidence=0.9)
        self.assertEqual(_evaluation_id(first), _evaluation_id(second))

    def test_build_metrics_derives_pipeline_metrics_and_custom_metrics(self) -> None:
        metrics = build_metrics(self._request(custom_metrics={"cer": 0.04}))

        self.assertAlmostEqual(metrics["confidence_mean"], 0.85)
        self.assertAlmostEqual(metrics["confidence_gap"], 0.1)
        self.assertAlmostEqual(metrics["field_completeness"], 0.75)
        self.assertEqual(metrics["outcome_delivered"], 1.0)
        self.assertAlmostEqual(metrics["cer"], 0.04)

    def test_build_parameters_and_tags_preserve_lineage(self) -> None:
        request = self._request(
            dataset_version="gold-v2",
            pipeline_version="release-7",
            parameters={"model": "paddleocr"},
            tags={"tenant": "default"},
        )
        parameters = build_parameters(request)
        tags = build_tags(request, "evaluation-1")

        self.assertEqual(parameters["dataset_version"], "gold-v2")
        self.assertEqual(parameters["pipeline_version"], "release-7")
        self.assertEqual(parameters["model"], "paddleocr")
        self.assertEqual(tags["evaluation_id"], "evaluation-1")
        self.assertEqual(tags["tenant"], "default")

    def test_provider_registry_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            build_provider("unknown")

    @patch("evaluation_service.app.main.upload_json")
    @patch("evaluation_service.app.main._persist_receipt")
    @patch("evaluation_service.app.main._load_receipt", return_value=None)
    @patch("evaluation_service.app.main._track_provider", new_callable=AsyncMock)
    def test_tracking_partial_success_is_non_blocking(
        self,
        mocked_track_provider,
        mocked_load_receipt,
        mocked_persist_receipt,
        mocked_upload_json,
    ) -> None:
        mocked_track_provider.side_effect = [
            {"provider": "mlflow", "status": "failed", "error": "offline", "attempts": 2},
            {"provider": "artifact_store", "status": "success", "artifact": "s3://evaluation-artifacts/run.json", "attempts": 1},
        ]
        mocked_persist_receipt.return_value = ("evaluation-artifacts", "jobs/job-1/evaluation/receipt.json")
        client = TestClient(app)
        response = client.post("/track-run", json=self._request().model_dump())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["tracked"])
        self.assertEqual(payload["tracking_status"], "partial_success")
        mocked_persist_receipt.assert_called_once()
        mocked_upload_json.assert_called_once()

    @patch("evaluation_service.app.main._load_receipt")
    def test_successful_receipt_returns_idempotent_replay(self, mocked_load_receipt) -> None:
        mocked_load_receipt.return_value = {
            "job_id": "job-1",
            "evaluation_id": "evaluation-existing",
            "tracking_status": "success",
            "provider_results": [{"provider": "mlflow", "status": "success", "run_id": "run-1"}],
            "evaluation_bucket": "evaluation-artifacts",
            "evaluation_receipt_key": "jobs/job-1/evaluation/evaluation-existing/receipt.json",
        }
        client = TestClient(app)
        response = client.post("/track-run", json=self._request().model_dump())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["idempotent_replay"])
        self.assertEqual(response.json()["mlflow_run_id"], "run-1")

    @patch("evaluation_service.app.main._load_receipt", return_value=None)
    def test_missing_evaluation_returns_404(self, mocked_load_receipt) -> None:
        client = TestClient(app)
        response = client.get("/evaluations/job-1/evaluation-missing")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
