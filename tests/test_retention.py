from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from scripts.enforce_retention import policy_from_env, retention_holds


class RetentionPolicyTests(unittest.TestCase):
    def test_protects_resolution_sources_children_and_pending_delivery(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [("ingestion_reprocess_requests",), ("delivery_operations",), ("delivery_work",)]
        cursor.fetchall.side_effect = [[("parent", "child")], [("delivery",)]]
        held, tables = retention_holds(cursor, apply=True)
        self.assertEqual(held, {"parent", "child", "delivery"})
        self.assertEqual(len(tables), 3)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(sum(sql.startswith("LOCK TABLE") for sql in statements), 4)
        self.assertTrue(all('ACCESS EXCLUSIVE' in sql for sql in statements if sql.startswith('LOCK TABLE')))
        self.assertIn("w.state='dead_letter'", statements[-1])

    def test_dry_run_does_not_lock_and_old_schema_is_supported(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(None,), (None,), (None,)]
        held, tables = retention_holds(cursor, apply=False)
        self.assertEqual(held, set())
        self.assertEqual(tables, set())
        self.assertEqual(cursor.execute.call_count, 3)

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
