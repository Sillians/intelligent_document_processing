import unittest
from unittest.mock import patch
import numpy as np
from ocr_service.app import main


class SecondaryOCRTests(unittest.TestCase):
    def test_acceptance_checks_text_confidence_geometry(self):
        token = {"text": "Invoice", "bbox": [[1, 1], [30, 1], [30, 10], [1, 10]]}
        self.assertTrue(main.valid_secondary_output([token], [0.95], (100, 100)))
        for scores in ([0.2], [float("nan")], []):
            self.assertFalse(main.valid_secondary_output([token], scores, (100, 100)))
        self.assertFalse(main.valid_secondary_output([{**token, "bbox": [[0, 0]] * 4}], [0.95], (100, 100)))

    def test_real_secondary_output_and_provenance_required(self):
        result = [{"rec_texts": ["Invoice"], "rec_scores": [0.95], "rec_polys": [[[1, 1], [30, 1], [30, 10], [1, 10]]]}]
        with patch.object(main.settings, "ocr_secondary_backend", "tesseract"), patch.object(main.settings, "ocr_backend", "paddle"), patch.object(main.settings, "ocr_force_fallback", False), patch.object(main, "_execute_ocr_sync", return_value=([], [], "engine_execution_failed")), patch.object(main, "_load_and_decode_image", return_value=np.zeros((100, 100, 3))), patch.object(main, "_init_tesseract_engine", return_value=(object(), {"backend": "tesseract", "version": "test"})), patch.object(main, "_run_engine", return_value=result):
            pages, reason = main._execute_document_ocr("job-1", [{"page_number": 1, "preprocessed_key": "jobs/job-1/page.png"}])
        self.assertEqual(reason, "")
        self.assertEqual(pages[0]["secondary_engine"]["backend"], "tesseract")
