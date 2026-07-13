from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.enforce_retention import policy_from_env


class RetentionPolicyTests(unittest.TestCase):
    def test_accepts_ordered_retention_periods(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RAW_ARTIFACT_RETENTION_DAYS": "30",
                "DERIVED_ARTIFACT_RETENTION_DAYS": "90",
                "AUDIT_RETENTION_DAYS": "365",
            },
            clear=False,
        ):
            policy = policy_from_env()

        self.assertEqual(policy.raw_days, 30)
        self.assertEqual(policy.audit_days, 365)

    def test_rejects_audit_retention_shorter_than_derived(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RAW_ARTIFACT_RETENTION_DAYS": "30",
                "DERIVED_ARTIFACT_RETENTION_DAYS": "90",
                "AUDIT_RETENTION_DAYS": "60",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "audit retention"):
                policy_from_env()


if __name__ == "__main__":
    unittest.main()
