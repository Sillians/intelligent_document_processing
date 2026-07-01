from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from ocr_service.app.main import (
    TesseractEngine,
    _configured_backend,
    _extract_tokens,
    _extract_tokens_from_legacy_page,
    _extract_tokens_from_v3_page,
    _fallback_text,
    _normalize_input_image,
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

    def test_fallback_text_prefers_reason(self) -> None:
        text = _fallback_text(True, "service_busy")
        self.assertEqual(text, "ocr_fallback:service_busy")

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

    def test_ready_accepts_explicit_fallback_mode(self) -> None:
        import asyncio

        with patch.object(settings, "ocr_force_fallback", True):
            payload = asyncio.run(ready())

        self.assertEqual(payload, {"status": "ready", "backend": "fallback"})


if __name__ == "__main__":
    unittest.main()
