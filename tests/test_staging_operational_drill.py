from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.staging_operational_drill import (
    DrillError,
    REQUIRED_ALERTS,
    hardening,
    observability,
    prometheus_query,
    restore_verify,
    smoke,
)


class StagingObservabilityTests(unittest.TestCase):
    @patch("scripts.staging_operational_drill.run_command")
    def test_smoke_failure_surfaces_e2e_output(self, mocked_command) -> None:
        completed = subprocess.CompletedProcess(
            args=["python", "scripts/full_pipeline_e2e.py"],
            returncode=1,
            stdout="workflow status: FAILED\n",
            stderr="E2E failed: workflow ended unsuccessfully: FAILED\n",
        )
        mocked_command.return_value = completed

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                api_url="http://127.0.0.1:8081",
                api_key="secret",
                tenant_id="default",
                actor_id="staging-smoke",
                require_observability=True,
                prometheus_url="http://127.0.0.1:9090",
                sample_path=None,
                timeout_seconds=1,
                poll_interval=0.01,
                artifact_dir=tmp,
            )

            with self.assertRaises(DrillError) as raised:
                smoke(args, Path("."))

            message = str(raised.exception)
            self.assertIn("E2E failed: workflow ended unsuccessfully: FAILED", message)
            self.assertIn("workflow status: FAILED", message)
            self.assertTrue((Path(tmp) / "drill_manifest.json").exists())

    @patch("scripts.staging_operational_drill.http_get_json")
    def test_prometheus_query_returns_vector(self, mocked_get) -> None:
        mocked_get.return_value = {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "1"]}]}}

        result = prometheus_query("http://prometheus:9090", 'up{job="idp"}', 5)

        self.assertEqual(len(result), 1)
        self.assertIn("query=up%7Bjob%3D%22idp%22%7D", mocked_get.call_args.args[0])

    @patch("scripts.staging_operational_drill.http_get_json")
    @patch("scripts.staging_operational_drill.prometheus_query")
    def test_observability_requires_targets_metrics_and_rules(self, mocked_query, mocked_get) -> None:
        mocked_query.side_effect = [
            [{}] * 10,
            [{}],
            [{}],
            [{}],
            [{}],
        ]
        mocked_get.side_effect = [
            {
                "status": "success",
                "data": {
                    "groups": [
                        {
                            "rules": [
                                {"type": "alerting", "name": alert_name}
                                for alert_name in sorted(REQUIRED_ALERTS)
                            ]
                        }
                    ]
                },
            },
            {"cluster": {"status": "ready"}},
            {"database": "ok"},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                prometheus_url="http://localhost:9090",
                alertmanager_url="http://localhost:9093",
                grafana_url="http://localhost:3000",
                min_pipeline_targets=10,
                wait_seconds=1,
                poll_interval=0.01,
                http_timeout=1,
                artifact_dir=tmp,
            )
            result = observability(args, Path("."))

            self.assertTrue(result["passed"])
            self.assertEqual(result["pipeline_target_count"], 10)
            self.assertTrue((Path(tmp) / "observability_manifest.json").exists())
            disk_query = mocked_query.call_args_list[4].args[1]
            self.assertIn("node_filesystem_avail_bytes", disk_query)
            self.assertNotIn('mountpoint="/"', disk_query)

    @patch("scripts.staging_operational_drill.run_command")
    @patch("scripts.staging_operational_drill.request_status")
    @patch("scripts.staging_operational_drill.submit_document")
    def test_hardening_verifies_tenant_boundaries_and_audit(
        self,
        mocked_submit,
        mocked_status,
        mocked_command,
    ) -> None:
        mocked_submit.return_value = {"job_id": "job-123"}
        mocked_status.side_effect = [401, 403, 404, 404]
        mocked_command.return_value.stdout = "5\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.png"
            sample.write_bytes(b"sample")
            env_file = root / ".env.staging"
            env_file.write_text("POSTGRES_USER=idp\nPOSTGRES_DB=idp_staging\n", encoding="utf-8")
            args = argparse.Namespace(
                api_url="http://127.0.0.1:8081",
                primary_api_key="primary-secret",
                primary_tenant="tenant-a",
                secondary_api_key="secondary-secret",
                secondary_tenant="tenant-b",
                sample_path=str(sample),
                http_timeout=5,
                allow_loopback_http=True,
                env_file=str(env_file),
                compose_file=None,
                artifact_dir=str(root / "evidence"),
                command_timeout=30,
            )

            result = hardening(args, root)

            self.assertTrue(result["passed"])
            self.assertEqual(result["audit_event_count"], 5)
            self.assertEqual(result["transport"], "loopback-http-waiver")

    def test_restore_verify_decrypts_backup_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backup"
            backup_dir.mkdir()
            postgres = root / "postgres.sql"
            postgres.write_text("-- PostgreSQL database dump\nCREATE DATABASE idp;\n", encoding="utf-8")
            volume_file = root / "data.txt"
            volume_file.write_text("persistent data", encoding="utf-8")
            archive = root / "postgres_data.tgz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(volume_file, arcname="data.txt")

            encryption_key = "backup-test-key-" + ("x" * 32)
            command_env = os.environ.copy()
            command_env["IDP_BACKUP_ENCRYPTION_KEY"] = encryption_key
            for source, destination in (
                (postgres, backup_dir / "postgres.sql.enc"),
                (archive, backup_dir / "postgres_data.tgz.enc"),
            ):
                subprocess.run(
                    [
                        "openssl",
                        "enc",
                        "-aes-256-cbc",
                        "-pbkdf2",
                        "-salt",
                        "-in",
                        str(source),
                        "-out",
                        str(destination),
                        "-pass",
                        "env:IDP_BACKUP_ENCRYPTION_KEY",
                    ],
                    check=True,
                    env=command_env,
                    capture_output=True,
                )
            env_file = root / ".env"
            env_file.write_text(f"BACKUP_ENCRYPTION_KEY={encryption_key}\n", encoding="utf-8")
            args = argparse.Namespace(
                backup_dir=str(backup_dir),
                env_file=str(env_file),
                command_timeout=30,
            )

            result = restore_verify(args, root)

            self.assertTrue(result["passed"])
            self.assertTrue(result["checks"]["postgres_data.tgz.enc"]["encrypted"])


if __name__ == "__main__":
    unittest.main()
