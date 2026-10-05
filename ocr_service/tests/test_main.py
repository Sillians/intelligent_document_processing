from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ocr_service.app.main import (
    TesseractEngine,
    _configured_backend,
    _extract_tokens,
    _extract_tokens_from_legacy_page,
    _extract_tokens_from_v3_page,
    app,
    _normalize_input_image,
    _execute_document_ocr,
    _run_engine,
    ready,
    settings,
)


class _PredictOnlyEngine:
    def predict(self, image: Any) -> list[dict[str, Any]]:
        return [{"rec_texts": ["Invoice", "INV-001"], "rec_scores": [0.92, 0.88], "rec_polys": [[[1, 1]]]}]

    def ocr(self, image: Any, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("cls argument unsupported")


class _FakePytesseract:
    class Output:
        DICT = "dict"

    @staticmethod
    def image_to_data(image: Any, **kwargs: Any) -> dict[str, list[Any]]:
        return {
            "text": ["", "Invoice", "INV-001"],
            "conf": ["-1", "92.5", "88"],
            "left": [0, 10, 80],
            "top": [0, 20, 20],
            "width": [0, 60, 70],
            "height": [0, 18, 18],
        }


class OCRServiceTests(unittest.TestCase):
    @patch("ocr_service.app.main._execute_ocr_sync")
    def test_document_ocr_keeps_page_order_and_fails_on_any_engine_error(self, execute) -> None:
        pages = [
            {"page_number": 1, "preprocessed_key": "jobs/j/preprocessed/page-0001.png"},
            {"page_number": 2, "preprocessed_key": "jobs/j/preprocessed/page-0002.png"},
        ]
        execute.side_effect = [([{"text": "Invoice"}], [0.9], ""), ([{"text": "Total"}], [0.8], "")]
        output, reason = _execute_document_ocr("j", pages)
        self.assertEqual(reason, "")
        self.assertEqual([page["page_number"] for page in output], [1, 2])
        execute.side_effect = [([{"text": "Invoice"}], [0.9], ""), ([], [], "engine_execution_failed")]
        output, reason = _execute_document_ocr("j", pages)
        self.assertEqual(output, [])
        self.assertEqual(reason, "page_2:engine_execution_failed")
    def test_run_engine_uses_compatible_method(self) -> None:
        result = _run_engine(_PredictOnlyEngine(), image=[[0]])
        self.assertIsInstance(result, list)
        self.assertTrue(result)

    def test_extract_tokens_from_legacy_page(self) -> None:
        page = [
            [[[1, 2], [3, 4]], ("Invoice", 0.95)],
            [[[5, 6], [7, 8]], ("INV-123", 0.90)],
        ]
        tokens, confidences = _extract_tokens_from_legacy_page(page)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0]["text"], "Invoice")
        self.assertAlmostEqual(confidences[0], 0.95, places=3)

    def test_extract_tokens_from_v3_page(self) -> None:
        page = {
            "rec_texts": ["Invoice", "INV-123"],
            "rec_scores": [0.93, 0.89],
            "rec_polys": [[[1, 2], [3, 4]], [[5, 6], [7, 8]]],
        }
        tokens, confidences = _extract_tokens_from_v3_page(page)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[1]["text"], "INV-123")
        self.assertAlmostEqual(confidences[1], 0.89, places=3)

    def test_extract_tokens_accepts_mixed_result(self) -> None:
        payload = [
            {"rec_texts": ["Total", "$540.00"], "rec_scores": [0.91, 0.87], "rec_polys": [[], []]},
            [[[1, 2], [3, 4]], ("Due", 0.80)],
        ]
        tokens, confidences = _extract_tokens(payload)
        self.assertGreaterEqual(len(tokens), 3)
        self.assertGreaterEqual(len(confidences), 3)

    @patch("ocr_service.app.main._execute_ocr_sync", return_value=([], [], "engine_execution_failed"))
    def test_ocr_engine_failure_does_not_persist_synthetic_text(self, _mocked_execute) -> None:
        with patch.object(settings, "ocr_force_fallback", False), patch.object(settings, "ocr_backend", "paddle"):
            response = TestClient(app).post("/ocr", json={"job_id": "job-1", "preprocessed_key": "jobs/job-1/preprocessed/page.png"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "OCR_UNAVAILABLE")
        self.assertTrue(response.json()["detail"]["retryable"])

    def test_normalize_input_image_gray_to_bgr(self) -> None:
        import numpy as np

        image = np.zeros((2, 2), dtype=np.uint8)
        converted = _normalize_input_image(image)
        self.assertEqual(converted.shape, (2, 2, 3))

    def test_tesseract_engine_normalizes_confidence_and_bounding_boxes(self) -> None:
        engine = TesseractEngine(_FakePytesseract(), language="eng", oem=1, psm=6)

        result = engine.predict([[0]])
        tokens, confidences = _extract_tokens(result)

        self.assertEqual([token["text"] for token in tokens], ["Invoice", "INV-001"])
        self.assertAlmostEqual(confidences[0], 0.925, places=3)
        self.assertEqual(tokens[0]["bbox"], [[10, 20], [70, 20], [70, 38], [10, 38]])

    def test_configured_backend_honors_force_fallback(self) -> None:
        with patch.object(settings, "ocr_backend", "tesseract"), patch.object(settings, "ocr_force_fallback", True):
            self.assertEqual(_configured_backend(), "fallback")

    def test_ready_rejects_explicit_fallback_mode(self) -> None:
        import asyncio

        with patch.object(settings, "ocr_force_fallback", True), self.assertRaises(HTTPException) as raised:
            asyncio.run(ready())
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
