import unittest

from scripts.quality_diagnostics import quality_breakdown


class QualityDiagnosticsTests(unittest.TestCase):
    def test_family_field_errors_and_calibration_are_labeled_only(self):
        result = quality_breakdown([
            {'document_family':'invoice', 'matched_fields':{'total':True}, 'populated_fields':{'total':True},
             'field_f1':1.0, 'extraction_confidence':0.9, 'validation_correct':None},
            {'document_family':'receipt', 'matched_fields':{'total':False}, 'populated_fields':{'total':True},
             'field_f1':0.0, 'extraction_confidence':0.9, 'validation_correct':False},
            {'document_family':'invoice', 'matched_fields':{}, 'extraction_confidence':1.0},
        ])
        self.assertIsNone(result['by_document_family']['invoice']['validation_accuracy'])
        self.assertEqual(result['by_field']['receipt']['total']['fp'], 1)
        self.assertEqual(result['by_field']['receipt']['total']['fn'], 1)
        self.assertEqual(result['document_confidence_calibration']['labeled_count'], 2)
        self.assertAlmostEqual(result['document_confidence_calibration']['ece'], 0.4)

    def test_no_labels_produces_no_invented_calibration(self):
        result = quality_breakdown([{'document_family':'unknown'}])
        self.assertIsNone(result['document_confidence_calibration']['ece'])
        self.assertEqual(result['by_field'], {})
