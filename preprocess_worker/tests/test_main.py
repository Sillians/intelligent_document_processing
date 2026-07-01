from __future__ import annotations

import unittest

import cv2
import numpy as np

from preprocess_worker.app.main import _ensure_odd, _estimate_skew_angle, _normalize_to_bgr, preprocess_image

HAS_FULL_OPENCV = all(
    hasattr(cv2, name)
    for name in (
        "adaptiveThreshold",
        "fastNlMeansDenoising",
        "minAreaRect",
        "putText",
        "rectangle",
        "threshold",
    )
)


class PreprocessWorkerTests(unittest.TestCase):
    def test_ensure_odd_enforces_odd_and_minimum(self) -> None:
        self.assertEqual(_ensure_odd(2), 3)
        self.assertEqual(_ensure_odd(10), 11)
        self.assertEqual(_ensure_odd(35), 35)

    @unittest.skipUnless(HAS_FULL_OPENCV, "OpenCV image processing runtime is not available")
    def test_estimate_skew_angle_blank_returns_zero(self) -> None:
        blank = np.full((200, 300), 255, dtype=np.uint8)
        angle = _estimate_skew_angle(blank, min_foreground_pixels=16)
        self.assertAlmostEqual(angle, 0.0, places=3)

    @unittest.skipUnless(HAS_FULL_OPENCV, "OpenCV image processing runtime is not available")
    def test_estimate_skew_angle_keeps_horizontal_invoice_upright(self) -> None:
        image = cv2.imread("samples/documents/sample_invoice_001.png", cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(image)

        angle = _estimate_skew_angle(image, min_foreground_pixels=64)

        self.assertAlmostEqual(angle, 0.0, delta=0.25)

    @unittest.skipUnless(HAS_FULL_OPENCV, "OpenCV image processing runtime is not available")
    def test_estimate_skew_angle_returns_small_corrective_rotation(self) -> None:
        image = np.full((400, 800), 255, dtype=np.uint8)
        for y in range(80, 340, 45):
            cv2.putText(
                image,
                "Invoice 12345 total 100.00",
                (60, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                0,
                2,
                cv2.LINE_AA,
            )
        matrix = cv2.getRotationMatrix2D((400, 200), 7.0, 1.0)
        rotated = cv2.warpAffine(image, matrix, (800, 400), borderValue=255)

        angle = _estimate_skew_angle(rotated, min_foreground_pixels=64)

        self.assertAlmostEqual(angle, -7.0, delta=0.5)

    @unittest.skipUnless(HAS_FULL_OPENCV, "OpenCV image processing runtime is not available")
    def test_estimate_skew_angle_rejects_implausible_rotation(self) -> None:
        image = np.full((300, 300), 255, dtype=np.uint8)
        cv2.line(image, (40, 260), (260, 40), 0, 8)

        angle = _estimate_skew_angle(
            image,
            min_foreground_pixels=64,
            max_correction_degrees=15.0,
        )

        self.assertEqual(angle, 0.0)

    @unittest.skipUnless(HAS_FULL_OPENCV, "OpenCV image processing runtime is not available")
    def test_preprocess_image_returns_bgr_and_metadata(self) -> None:
        image = np.full((260, 720, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "Invoice INV-123",
            (30, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.3,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )

        processed, metadata = preprocess_image(
            image,
            max_dimension=2200,
            denoise_h=8,
            threshold_block_size=35,
            threshold_c=11,
            enable_clahe=True,
            min_foreground_pixels=64,
        )

        self.assertEqual(processed.ndim, 3)
        self.assertEqual(processed.shape[2], 3)
        self.assertIn("deskew_angle", metadata)
        self.assertIn("pipeline", metadata)
        self.assertIn("foreground_pixels", metadata)
        self.assertIn("original_width", metadata)
        self.assertGreaterEqual(metadata["height"], 1)
        self.assertGreaterEqual(metadata["width"], 1)

    def test_normalize_to_bgr_handles_grayscale_and_alpha(self) -> None:
        gray = np.zeros((4, 5), dtype=np.uint8)
        alpha = np.zeros((4, 5, 4), dtype=np.uint8)

        self.assertEqual(_normalize_to_bgr(gray).shape, (4, 5, 3))
        self.assertEqual(_normalize_to_bgr(alpha).shape, (4, 5, 3))

    @unittest.skipUnless(HAS_FULL_OPENCV, "OpenCV image processing runtime is not available")
    def test_preprocess_image_respects_disabled_optional_steps(self) -> None:
        image = np.full((80, 120, 3), 255, dtype=np.uint8)
        cv2.rectangle(image, (10, 20), (90, 50), (0, 0, 0), -1)

        _, metadata = preprocess_image(
            image,
            max_dimension=2200,
            denoise_h=0,
            threshold_block_size=34,
            threshold_c=11,
            enable_clahe=False,
            min_foreground_pixels=64,
            enable_deskew=False,
            enable_threshold=False,
            median_blur_kernel=1,
        )

        self.assertNotIn("clahe", metadata["pipeline"])
        self.assertNotIn("adaptive_threshold", metadata["pipeline"])
        self.assertEqual(metadata["threshold_block_size"], 35)
        self.assertEqual(metadata["median_blur_kernel"], 1)


if __name__ == "__main__":
    unittest.main()
