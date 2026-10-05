from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from workflow_orchestrator.app.activities import (
    _is_non_retryable_status,
    ocr_activity,
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

    @patch("workflow_orchestrator.app.activities._post", new_callable=AsyncMock)
    def test_ocr_network_error_propagates_for_temporal_retry(self, mocked_post: AsyncMock) -> None:
        mocked_post.side_effect = RuntimeError("OCR unavailable")
        from temporalio.exceptions import ApplicationError
        with self.assertRaises(ApplicationError) as raised:
            asyncio.run(ocr_activity({"job_id": "job-123"}))
        self.assertEqual(raised.exception.type, "OCR_UNAVAILABLE")
        self.assertFalse(raised.exception.non_retryable)


if __name__ == "__main__":
    unittest.main()
