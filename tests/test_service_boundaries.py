from __future__ import annotations

import unittest

from scripts.assess_service_boundaries import assess


class ServiceBoundaryAssessmentTests(unittest.TestCase):
    def test_insufficient_evidence_does_not_recommend_merges(self) -> None:
        report = assess({"metrics": {"sample_count": 1, "stage_p95_ms": {"classification": 1}}, "quality_gate": {"passed": False}})
        self.assertFalse(report["representative_gate_passed"])
        self.assertTrue(report["decisions"]["classifier_router_service"].startswith("defer"))

    def test_representative_low_compute_is_only_candidate(self) -> None:
        report = assess({"metrics": {"sample_count": 25, "document_family_count": 3, "multi_page_sample_count": 5, "stage_p95_ms": {"classification": 3, "validation": 5}}, "quality_gate": {"passed": True}})
        self.assertTrue(report["representative_gate_passed"])
        self.assertTrue(report["decisions"]["validation_service"].startswith("candidate_to_combine"))
