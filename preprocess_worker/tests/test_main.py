from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from preprocess_worker.app.main import PreprocessRequest, _decode_pages, _ensure_odd, _estimate_skew_angle, _normalize_to_bgr, _preprocess_sync, preprocess_image, settings

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
    @patch('preprocess_worker.app.main.upload_json')
    @patch('preprocess_worker.app.main.upload_bytes')
    @patch('preprocess_worker.app.main.download_bytes')
    def test_pdf_text_layer_reference_is_separate_from_rendered_pixels(self, download, upload, native_upload):
        output = BytesIO()
        Image.new('RGB', (72, 72), 'white').save(output, format='PDF')
        download.return_value = output.getvalue()
        request = PreprocessRequest(job_id='job-1', raw_bucket=settings.raw_bucket, raw_key='jobs/job-1/raw/source.pdf')
        result = _preprocess_sync(request)
        self.assertEqual(result['page_count'], 1)
        self.assertEqual(result['pages'][0]['native_text_key'], 'jobs/job-1/preprocessed/native-0001.json')
        self.assertNotIn('native_text', result['pages'][0])
        self.assertEqual(native_upload.call_args.args[3]['coordinate_space'], 'pdf-points')
        self.assertFalse(native_upload.call_args.args[3]['truncated'])

    def test_total_pixel_budget_rejects_document_before_remaining_decodes(self):
        from fastapi import HTTPException
        output = BytesIO()
        Image.new('RGB', (20, 10)).save(output, format='TIFF', save_all=True,
                                      append_images=[Image.new('RGB', (20, 10))])
        with patch.object(settings, 'preprocess_max_total_pixels', 300), self.assertRaises(HTTPException):
            _decode_pages(output.getvalue())
    @patch("preprocess_worker.app.main.upload_bytes")
    @patch("preprocess_worker.app.main.download_bytes")
    def test_preprocess_persists_each_tiff_page(self, download, upload) -> None:
        output = BytesIO()
        Image.new("RGB", (20, 10), "white").save(output, format="TIFF", save_all=True, append_images=[Image.new("RGB", (30, 15), "white")])
        download.return_value = output.getvalue()
        request = PreprocessRequest(job_id="job-1", raw_bucket=settings.raw_bucket, raw_key="jobs/job-1/raw/source.tiff")
        with patch.object(settings, "preprocess_max_pages", 2):
            result = _preprocess_sync(request)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual([page["page_number"] for page in result["pages"]], [1, 2])
        self.assertEqual(upload.call_count, 2)
        self.assertEqual([call.args[2].split("/")[-1] for call in upload.call_args_list], ["page-0001.png", "page-0002.png"])

    def test_multipage_tiff_decodes_every_page_in_order(self) -> None:
        output = BytesIO()
        Image.new("RGB", (20, 10), "red").save(output, format="TIFF", save_all=True, append_images=[Image.new("RGB", (30, 15), "blue")])
        with patch.object(settings, "preprocess_max_pages", 2):
            pages = _decode_pages(output.getvalue())
        self.assertEqual([page.shape[:2] for page in pages], [(10, 20), (15, 30)])

    def test_page_cap_rejects_multipage_tiff(self) -> None:
        from fastapi import HTTPException
        output = BytesIO()
        Image.new("RGB", (20, 10), "red").save(output, format="TIFF", save_all=True, append_images=[Image.new("RGB", (30, 15), "blue")])
        with patch.object(settings, "preprocess_max_pages", 1), self.assertRaises(HTTPException) as raised:
            _decode_pages(output.getvalue())
        self.assertEqual(raised.exception.status_code, 422)

    def test_multipage_pdf_renders_every_page(self) -> None:
        output = BytesIO()
        Image.new("RGB", (72, 72), "white").save(output, format="PDF", save_all=True, append_images=[Image.new("RGB", (100, 72), "white")])
        with patch.object(settings, "preprocess_max_pages", 2), patch.object(settings, "preprocess_pdf_dpi", 72):
            pages = _decode_pages(output.getvalue())
        self.assertEqual(len(pages), 2)
        self.assertNotEqual(pages[0].shape[1], pages[1].shape[1])

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
