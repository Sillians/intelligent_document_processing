from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from workflow_orchestrator.app.pipeline import (
    PipelineContractError,
    build_classification_payload,
    build_delivery_payload,
    build_evaluation_payload,
    build_extraction_payload,
    build_webhook_event_payload,
    build_layout_payload,
    build_ocr_payload,
    build_preprocess_payload,
    build_review_payload,
    build_validation_payload,
    should_route_to_human_review,
)

with workflow.unsafe.imports_passed_through():
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


PIPELINE_TIMEOUT = timedelta(minutes=5)
SHORT_STAGE_TIMEOUT = timedelta(seconds=30)
DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn
class DocumentPipelineWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id", "unknown"))
        tenant_id = str(payload.get("tenant_id") or "default")
        workflow_id = workflow.info().workflow_id
        workflow.logger.info("workflow started job_id=%s", job_id)

        final_status = "failed"
        preprocessed: dict[str, Any] | None = None
        ocr: dict[str, Any] | None = None
        layout: dict[str, Any] | None = None
        classification: dict[str, Any] | None = None
        extraction: dict[str, Any] | None = None
        validation: dict[str, Any] | None = None
        branch_payload: dict[str, Any] = {}

        try:
            preprocess_payload = build_preprocess_payload(payload)
            job_id = preprocess_payload["job_id"]

            preprocessed = await workflow.execute_activity(
                preprocess_activity,
                preprocess_payload,
                start_to_close_timeout=PIPELINE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            ocr = await workflow.execute_activity(
                ocr_activity,
                build_ocr_payload(job_id, preprocessed),
                start_to_close_timeout=PIPELINE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            layout = await workflow.execute_activity(
                layout_activity,
                build_layout_payload(job_id, preprocessed, ocr),
                start_to_close_timeout=PIPELINE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            classification = await workflow.execute_activity(
                classify_activity,
                build_classification_payload(job_id, ocr),
                start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            extraction = await workflow.execute_activity(
                extract_activity,
                build_extraction_payload(job_id, ocr, layout, classification),
                start_to_close_timeout=PIPELINE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            validation = await workflow.execute_activity(
                validate_activity,
                build_validation_payload(job_id, extraction),
                start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            if should_route_to_human_review(validation):
                review = await workflow.execute_activity(
                    create_review_task_activity,
                    build_review_payload(job_id, extraction, validation),
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
                final_status = "pending_human_review"
                branch_payload = {"review_task": review}
            else:
                delivery = await workflow.execute_activity(
                    deliver_activity,
                    build_delivery_payload(job_id, extraction, validation),
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
                final_status = "delivered"
                branch_payload = {"delivery": delivery}

            result = {
                "job_id": job_id,
                "status": final_status,
                "classification": classification,
                "preprocess": preprocessed,
                "ocr": ocr,
                "layout": layout,
                "extraction": extraction,
                "validation": validation,
                **branch_payload,
            }
            webhook_event_type = (
                "document.pending_human_review"
                if final_status == "pending_human_review"
                else "document.completed"
            )
            try:
                await workflow.execute_activity(
                    notify_webhook_activity,
                    build_webhook_event_payload(
                        event_type=webhook_event_type,
                        tenant_id=tenant_id,
                        job_id=job_id,
                        workflow_id=workflow_id,
                        final_status=final_status,
                        result=result,
                    ),
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "webhook notification failed job_id=%s status=%s error=%s",
                    job_id,
                    final_status,
                    str(exc),
                )
            workflow.logger.info("workflow completed job_id=%s status=%s", job_id, final_status)
            return result

        except PipelineContractError as exc:
            try:
                await workflow.execute_activity(
                    notify_webhook_activity,
                    build_webhook_event_payload(
                        event_type="document.failed",
                        tenant_id=tenant_id,
                        job_id=job_id,
                        workflow_id=workflow_id,
                        final_status="failed",
                        error=str(exc),
                    ),
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
            except Exception as notify_exc:  # noqa: BLE001
                workflow.logger.warning(
                    "failed webhook notification failed job_id=%s error=%s",
                    job_id,
                    str(notify_exc),
                )
            # Contract violations should not retry indefinitely; fail workflow deterministically.
            raise ApplicationError(str(exc), type="PipelineContractError", non_retryable=True) from exc

        except Exception as exc:
            try:
                await workflow.execute_activity(
                    notify_webhook_activity,
                    build_webhook_event_payload(
                        event_type="document.failed",
                        tenant_id=tenant_id,
                        job_id=job_id,
                        workflow_id=workflow_id,
                        final_status="failed",
                        error=str(exc),
                    ),
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
            except Exception as notify_exc:  # noqa: BLE001
                workflow.logger.warning(
                    "failed webhook notification failed job_id=%s error=%s",
                    job_id,
                    str(notify_exc),
                )
            raise

        finally:
            evaluation_payload = build_evaluation_payload(
                job_id=job_id,
                final_status=final_status,
                ocr_result=ocr,
                extraction_result=extraction,
                validation_result=validation,
            )

            try:
                await workflow.execute_activity(
                    evaluate_activity,
                    evaluation_payload,
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                # Evaluation should not block delivery/review outcomes.
                workflow.logger.warning(
                    "evaluation tracking failed job_id=%s status=%s error=%s",
                    job_id,
                    final_status,
                    str(exc),
                )
