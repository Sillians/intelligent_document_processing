from __future__ import annotations

import unittest
from unittest.mock import patch

from workflow_orchestrator.app.activities import (
    _build_ocr_fallback_response,
    _is_non_retryable_status,
    _is_ocr_fallback_error,
)


class ActivityHttpClassificationTests(unittest.TestCase):
    def test_4xx_non_retryable_by_default(self) -> None:
        self.assertTrue(_is_non_retryable_status(400))
        self.assertTrue(_is_non_retryable_status(401))
        self.assertTrue(_is_non_retryable_status(404))

    def test_408_and_429_are_retryable(self) -> None:
        self.assertFalse(_is_non_retryable_status(408))
        self.assertFalse(_is_non_retryable_status(429))

    def test_5xx_retryable(self) -> None:
        self.assertFalse(_is_non_retryable_status(500))
        self.assertFalse(_is_non_retryable_status(503))

    def test_ocr_fallback_error_detection(self) -> None:
        self.assertTrue(_is_ocr_fallback_error("ocr: network error calling http://ocr-service:8011/ocr"))
        self.assertTrue(_is_ocr_fallback_error("ocr: downstream HTTP 503 from x"))
        self.assertFalse(_is_ocr_fallback_error("ocr: downstream HTTP 400 from x"))

    @patch("workflow_orchestrator.app.activities.upload_json")
    def test_build_ocr_fallback_response_persists_payload(self, mocked_upload_json) -> None:
        result = _build_ocr_fallback_response("job-123", "downstream_unavailable")
        self.assertEqual(result["job_id"], "job-123")
        self.assertEqual(result["token_count"], 1)
        self.assertTrue(result["fallback_used"])
        mocked_upload_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
