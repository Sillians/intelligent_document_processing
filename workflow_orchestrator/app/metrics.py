from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any

from prometheus_client import Gauge, start_http_server
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

TASK_QUEUE_BACKLOG = Gauge(
    "idp_temporal_task_queue_backlog",
    "Approximate number of pending Temporal tasks",
    ["namespace", "task_queue", "task_type"],
)
TASK_QUEUE_POLLERS = Gauge(
    "idp_temporal_task_queue_pollers",
    "Number of Temporal pollers recently seen for the task queue",
    ["namespace", "task_queue", "task_type"],
)
TASK_QUEUE_METRICS_LAST_SUCCESS = Gauge(
    "idp_temporal_task_queue_metrics_last_success_timestamp_seconds",
    "Unix timestamp of the last successful Temporal task queue metrics collection",
    ["namespace", "task_queue"],
)

_TASK_TYPES = (
    ("workflow", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
    ("activity", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
)


def start_metrics_server() -> None:
    port = int(os.getenv("WORKFLOW_METRICS_PORT", "9091"))
    start_http_server(port)


async def collect_task_queue_metrics(client: Any, namespace: str, task_queue: str) -> None:
    for task_type_name, task_type in _TASK_TYPES:
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=namespace,
                task_queue=TaskQueue(name=task_queue),
                task_queue_type=task_type,
                include_task_queue_status=True,
            ),
            timeout=timedelta(seconds=10),
        )
        TASK_QUEUE_BACKLOG.labels(namespace, task_queue, task_type_name).set(
            response.task_queue_status.backlog_count_hint
        )
        TASK_QUEUE_POLLERS.labels(namespace, task_queue, task_type_name).set(len(response.pollers))
    TASK_QUEUE_METRICS_LAST_SUCCESS.labels(namespace, task_queue).set_to_current_time()


async def monitor_task_queue(
    client: Any,
    namespace: str,
    task_queue: str,
    *,
    interval_seconds: float | None = None,
) -> None:
    logger = logging.getLogger("workflow_orchestrator.metrics")
    interval = interval_seconds or float(os.getenv("WORKFLOW_METRICS_INTERVAL_SECONDS", "15"))
    while True:
        try:
            await collect_task_queue_metrics(client, namespace, task_queue)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Failed to collect Temporal task queue metrics")
        await asyncio.sleep(max(1.0, interval))
