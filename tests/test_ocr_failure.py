import unittest
from shared.idp_common.ocr_failure import ocr_failure


class OCRFailureTests(unittest.TestCase):
    def test_page_invalid_image_is_terminal(self):
        error = ocr_failure("page_2:invalid_image")
        self.assertEqual(error.status_code, 422)
        self.assertFalse(error.detail["retryable"])

    def test_transient_and_configuration_failures(self):
        for reason in ("request_timeout", "busy", "engine_execution_failed"):
            self.assertTrue(ocr_failure(reason).detail["retryable"])
        self.assertFalse(ocr_failure("disabled").detail["retryable"])
