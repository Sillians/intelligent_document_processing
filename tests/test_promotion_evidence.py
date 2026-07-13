from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_promotion_evidence import THRESHOLD_METRICS, verify_evidence


class PromotionEvidenceTests(unittest.TestCase):
    def test_accepts_matching_sha_and_approved_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir, policy_path, sha = self._write_fixture(Path(tmp))

            receipt = verify_evidence(artifact_dir, sha, policy_path)

        self.assertTrue(receipt["quality_gate_passed"])
        self.assertEqual(receipt["sample_count"], 20)

    def test_rejects_wrong_sha_and_bootstrap_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir, policy_path, _ = self._write_fixture(Path(tmp), policy_status="bootstrap")

            with self.assertRaisesRegex(ValueError, "policy status must be approved"):
                verify_evidence(artifact_dir, "b" * 40, policy_path)

    def _write_fixture(self, root: Path, policy_status: str = "approved") -> tuple[Path, Path, str]:
        artifact_dir = root / "artifact"
        artifact_dir.mkdir()
        sha = "a" * 40
        thresholds = {
            "min_sample_count": 20,
            "min_completion_rate": 0.95,
            "min_contract_pass_rate": 0.95,
            "min_route_accuracy": 0.85,
            "min_field_f1": 0.7,
            "min_validation_accuracy": 0.85,
            "min_throughput_documents_per_minute": 0.5,
            "max_human_review_rate": 0.5,
            "max_ocr_cer": 0.3,
            "max_ocr_wer": 0.45,
            "max_p95_latency_seconds": 180,
        }
        checks = [
            {
                "metric": metric_name,
                "threshold": thresholds[policy_key],
                "passed": True,
            }
            for policy_key, metric_name in THRESHOLD_METRICS.items()
        ]
        (artifact_dir / "config.json").write_text(json.dumps({"pipeline_version": sha}), encoding="utf-8")
        (artifact_dir / "quality_gate.json").write_text(
            json.dumps({"enabled": True, "passed": True, "checks": checks}),
            encoding="utf-8",
        )
        (artifact_dir / "summary.json").write_text(
            json.dumps(
                {
                    "error_count": 0,
                    "metrics": {"sample_count": 20},
                    "quality_gate": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        policy_path = root / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "profile": "test",
                    "status": policy_status,
                    "thresholds": thresholds,
                }
            ),
            encoding="utf-8",
        )
        return artifact_dir, policy_path, sha


if __name__ == "__main__":
    unittest.main()
