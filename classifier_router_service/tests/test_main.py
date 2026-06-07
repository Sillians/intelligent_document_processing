from __future__ import annotations

import unittest

from classifier_router_service.app.main import _extract_ocr_text, classify_document


class ClassifierRouterTests(unittest.TestCase):
    def test_extract_ocr_text_uses_tokens_and_confidence(self) -> None:
        text, token_count, mean_confidence, fallback_used = _extract_ocr_text(
            {
                "tokens": [
                    {"text": "Invoice", "confidence": 0.9},
                    {"text": "INV-001", "confidence": 0.8},
                ]
            }
        )

        self.assertEqual(text, "Invoice INV-001")
        self.assertEqual(token_count, 2)
        self.assertAlmostEqual(mean_confidence, 0.85, places=3)
        self.assertFalse(fallback_used)

    def test_invoice_document_routes_to_invoice_profile(self) -> None:
        decision = classify_document(
            "Invoice Number INV-001 Bill To Acme Corp Amount Due $500 Payment Terms Net 30",
            token_count=12,
            mean_ocr_confidence=0.92,
            fallback_used=False,
        )

        self.assertEqual(decision["route"], "invoice")
        self.assertEqual(decision["strategy_profile"], "invoice_v1")
        self.assertTrue(decision["auto_route"])
        self.assertGreater(decision["matched_signal_count"], 0)

    def test_ocr_fallback_routes_to_generic_review(self) -> None:
        decision = classify_document(
            "ocr_fallback:forced_fallback",
            token_count=1,
            mean_ocr_confidence=0.2,
            fallback_used=True,
        )

        self.assertEqual(decision["route"], "generic")
        self.assertFalse(decision["auto_route"])
        self.assertTrue(decision["requires_review"])
        self.assertEqual(decision["reason"], "ocr_fallback")

    def test_weak_text_routes_to_generic_review(self) -> None:
        decision = classify_document(
            "memo",
            token_count=1,
            mean_ocr_confidence=0.95,
            fallback_used=False,
        )

        self.assertEqual(decision["route"], "generic")
        self.assertEqual(decision["confidence_band"], "low")

    def test_contract_routes_to_contract_profile(self) -> None:
        decision = classify_document(
            "Service Agreement between Party A and Party B. Terms and signature are below.",
            token_count=13,
            mean_ocr_confidence=0.88,
            fallback_used=False,
        )

        self.assertEqual(decision["route"], "contract")
        self.assertEqual(decision["extraction_mode"], "layout_aware_vlm")


if __name__ == "__main__":
    unittest.main()
