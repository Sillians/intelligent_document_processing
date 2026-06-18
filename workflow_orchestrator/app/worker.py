from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from shared.idp_common.config import get_settings
from workflow_orchestrator.app.activities import (
    classify_activity,
    create_review_task_activity,
    deliver_activity,
    evaluate_activity,
    extract_activity,
    layout_activity,
    notify_webhook_activity,
    ocr_activity,
    preprocess_activity,
    validate_activity,
)
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _on_fatal_error(exc: BaseException) -> None:
    logger = logging.getLogger("workflow_orchestrator.worker")
    logger.exception("Temporal worker fatal error", exc_info=exc)


async def main() -> None:
    _configure_logging()
    logger = logging.getLogger("workflow_orchestrator.worker")
    settings = get_settings()

    logger.info(
        "Connecting worker to Temporal address=%s namespace=%s task_queue=%s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.temporal_task_queue,
    )

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[DocumentPipelineWorkflow],
        activities=[
            preprocess_activity,
            ocr_activity,
            layout_activity,
            classify_activity,
            extract_activity,
            validate_activity,
            create_review_task_activity,
            deliver_activity,
            evaluate_activity,
            notify_webhook_activity,
        ],
        identity=settings.temporal_worker_identity or None,
        max_cached_workflows=settings.temporal_worker_max_cached_workflows,
        max_concurrent_workflow_tasks=settings.temporal_worker_max_concurrent_workflow_tasks,
        max_concurrent_activities=settings.temporal_worker_max_concurrent_activities,
        graceful_shutdown_timeout=timedelta(seconds=settings.temporal_worker_graceful_shutdown_seconds),
        on_fatal_error=_on_fatal_error,
    )

    logger.info("Temporal worker started")
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("workflow_orchestrator.worker").info("Worker interrupted; shutting down")
