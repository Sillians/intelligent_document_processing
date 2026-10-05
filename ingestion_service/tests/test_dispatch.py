import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ingestion_service.app.main import _dispatch_claimed
from shared.idp_common.config import Settings
from temporalio.exceptions import WorkflowAlreadyStartedError


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(_env_file=None)
        self.dispatch = {
            "job_id": "job-1", "tenant_id": "tenant-a", "raw_bucket": "raw-documents",
            "raw_key": "tenants/tenant-a/jobs/job-1/raw/invoice.png", "attempts": 1, "lease_token": "lease-1",
        }

    @patch("ingestion_service.app.main._audit", new_callable=AsyncMock)
    @patch("ingestion_service.app.main.get_temporal_client", new_callable=AsyncMock)
    def test_dispatch_marks_outbox_complete_after_workflow_start(self, client, _audit) -> None:
        handle = Mock(first_execution_run_id="run-1")
        client.return_value.start_workflow = AsyncMock(return_value=handle)
        repository = Mock()
        self.assertTrue(asyncio.run(_dispatch_claimed(repository, self.settings, self.dispatch)))
        repository.mark_queued.assert_called_once_with("job-1", "idp-tenant-a-job-1", "run-1", "lease-1")
        payload = client.return_value.start_workflow.call_args.args[1]
        self.assertEqual(payload["tenant_id"], "tenant-a")

    @patch("ingestion_service.app.main.get_temporal_client", new_callable=AsyncMock)
    def test_dispatch_failure_remains_retryable(self, client) -> None:
        client.side_effect = ConnectionError("Temporal unavailable")
        repository = Mock()
        self.assertFalse(asyncio.run(_dispatch_claimed(repository, self.settings, self.dispatch)))
        repository.mark_failed.assert_not_called()
        repository.release_dispatch.assert_called_once()

    @patch("ingestion_service.app.main.get_temporal_client", new_callable=AsyncMock)
    def test_lost_acknowledgement_reuses_the_existing_run(self, client) -> None:
        client.return_value.start_workflow = AsyncMock(side_effect=WorkflowAlreadyStartedError(
            "idp-tenant-a-job-1", "DocumentPipelineWorkflow", run_id="existing-run"))
        repository = Mock()
        self.assertTrue(asyncio.run(_dispatch_claimed(repository, self.settings, self.dispatch)))
        repository.mark_queued.assert_called_once_with("job-1", "idp-tenant-a-job-1", "existing-run", "lease-1")
