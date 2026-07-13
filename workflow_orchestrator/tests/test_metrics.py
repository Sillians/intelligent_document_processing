from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from prometheus_client import REGISTRY

from workflow_orchestrator.app.metrics import collect_task_queue_metrics


class TaskQueueMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_workflow_and_activity_backlog(self) -> None:
        describe = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    task_queue_status=SimpleNamespace(backlog_count_hint=7),
                    pollers=[object(), object()],
                ),
                SimpleNamespace(
                    task_queue_status=SimpleNamespace(backlog_count_hint=3),
                    pollers=[object()],
                ),
            ]
        )
        client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=describe))

        await collect_task_queue_metrics(client, "default", "idp-pipeline")

        backlog = REGISTRY.get_sample_value(
            "idp_temporal_task_queue_backlog",
            {"namespace": "default", "task_queue": "idp-pipeline", "task_type": "workflow"},
        )
        activity_pollers = REGISTRY.get_sample_value(
            "idp_temporal_task_queue_pollers",
            {"namespace": "default", "task_queue": "idp-pipeline", "task_type": "activity"},
        )
        self.assertEqual(backlog, 7)
        self.assertEqual(activity_pollers, 1)
        self.assertEqual(describe.await_count, 2)


if __name__ == "__main__":
    unittest.main()
