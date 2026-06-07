from __future__ import annotations

import unittest

from layout_service.app.main import (
    _assign_tokens_to_blocks,
    _bbox_from_value,
    _heuristic_blocks_from_tokens,
    _normalize_ocr_tokens,
)


class LayoutServiceTests(unittest.TestCase):
    def test_bbox_from_polygon(self) -> None:
        bbox = _bbox_from_value([[10, 20], [30, 20], [30, 40], [10, 40]])
        self.assertEqual(bbox, (10.0, 20.0, 30.0, 40.0))

    def test_normalize_ocr_tokens_drops_tokens_without_boxes(self) -> None:
        payload = {
            "tokens": [
                {"text": "Invoice", "bbox": [[10, 20], [80, 20], [80, 40], [10, 40]], "confidence": 0.9},
                {"text": "NoBox", "confidence": 0.8},
                {"text": "", "bbox": [1, 2, 3, 4], "confidence": 0.1},
            ]
        }

        tokens = _normalize_ocr_tokens(payload, width=200, height=100)
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["text"], "Invoice")
        self.assertEqual(tokens[0]["x1"], 10)

    def test_heuristic_blocks_group_tokens_into_lines(self) -> None:
        tokens = [
            {"index": 0, "text": "Invoice", "confidence": 0.9, "x1": 10, "y1": 10, "x2": 80, "y2": 25, "cx": 45, "cy": 17},
            {"index": 1, "text": "Total", "confidence": 0.8, "x1": 10, "y1": 70, "x2": 60, "y2": 85, "cx": 35, "cy": 77},
        ]

        blocks = _heuristic_blocks_from_tokens(tokens, width=200, height=120)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["source"], "heuristic_ocr_line")

    def test_assign_tokens_to_blocks_adds_text_and_order(self) -> None:
        blocks = [{"id": "b0000", "type": "text", "x1": 0, "y1": 0, "x2": 100, "y2": 50, "score": 0.8, "source": "test"}]
        tokens = [
            {"index": 0, "text": "Invoice", "confidence": 0.9, "x1": 10, "y1": 10, "x2": 50, "y2": 20, "cx": 30, "cy": 15},
            {"index": 1, "text": "INV-001", "confidence": 0.85, "x1": 55, "y1": 10, "x2": 95, "y2": 20, "cx": 75, "cy": 15},
        ]

        enriched = _assign_tokens_to_blocks(blocks, tokens)
        self.assertEqual(enriched[0]["text"], "Invoice INV-001")
        self.assertEqual(enriched[0]["reading_order"], 0)
        self.assertAlmostEqual(enriched[0]["mean_confidence"], 0.875, places=3)


if __name__ == "__main__":
    unittest.main()
