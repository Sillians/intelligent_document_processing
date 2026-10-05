from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, ActivityError

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
    from fastapi import HTTPException
    from shared.idp_common.page_contract import validate_pages
    from workflow_orchestrator.app.page_execution import ocr_page_activity, layout_page_activity, aggregate_pages_activity
    from workflow_orchestrator.app.activities import (
        classify_activity,
        create_review_task_activity,
        deliver_activity,
        poll_delivery_activity,
        evaluate_activity,
        extract_activity,
        layout_activity,
        notify_webhook_activity,
        ocr_activity,
        poll_review_activity,
        record_ocr_exception_activity,
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
    async def _execute_pages(self, preprocessed: dict, tenant_id: str, job_id: str) -> dict:
        try:
            pages = validate_pages(preprocessed['pages'], tenant_id=tenant_id,
                                   job_id=job_id, key_field='preprocessed_key')
        except HTTPException as exc:
            raise PipelineContractError(str(exc.detail)) from exc
        results = []
        # Sequential admission bounds CPU/memory; each activity has its own durable retry/history.
        for number, page in enumerate(pages, 1):
            self._current_page = number
            if page.get('page_number') != number:
                raise PipelineContractError('Pages must be ordered and unique')
            request = {'tenant_id': tenant_id, 'job_id': job_id, 'page_slot': number,
                       'preprocessed_key': page['preprocessed_key']}
            ocr = await workflow.execute_activity(ocr_page_activity, request,
                activity_id=f'ocr-page-{number:04d}', start_to_close_timeout=PIPELINE_TIMEOUT, retry_policy=DEFAULT_RETRY)
            layout = await workflow.execute_activity(layout_page_activity, {**request, 'ocr_key': ocr['ocr_key']},
                activity_id=f'layout-page-{number:04d}', start_to_close_timeout=PIPELINE_TIMEOUT, retry_policy=DEFAULT_RETRY)
            results.append({'page_number': number, 'ocr': ocr, 'layout': layout})
        return await workflow.execute_activity(aggregate_pages_activity,
            {'tenant_id': tenant_id, 'job_id': job_id, 'pages': preprocessed['pages'], 'results': results},
            start_to_close_timeout=PIPELINE_TIMEOUT, retry_policy=DEFAULT_RETRY)

    def __init__(self) -> None:
        self._business_status = "processing"
        self._current_page = None
        self._review_snapshot: dict[str, Any] | None = None

    @workflow.query
    def business_status(self) -> str:
        return self._business_status

    @workflow.query
    def review_snapshot(self) -> dict[str, Any] | None:
        return self._review_snapshot

    async def _deliver_until_acknowledged(self, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        if config.get('reprocess_parent_job_id'):
            request = {**request, 'metadata': {**request.get('metadata', {}),
                       'reprocess_parent_job_id': config['reprocess_parent_job_id'],
                       'reprocess_generation': config.get('reprocess_generation')}}
        if not workflow.patched("durable-delivery-v1"):
            return await workflow.execute_activity(deliver_activity, request,
                start_to_close_timeout=SHORT_STAGE_TIMEOUT, retry_policy=DEFAULT_RETRY)
        deadline = workflow.now() + timedelta(seconds=max(60, int(config.get('delivery_wait_timeout_seconds', 43200))))
        interval = max(1, int(config.get('delivery_poll_interval_seconds', 10)))
        result = None
        while workflow.now() < deadline:
            try:
                if result and result.get('delivery_id'):
                    result = await workflow.execute_activity(poll_delivery_activity,
                        {'tenant_id': request['tenant_id'], 'job_id': request['job_id'], 'delivery_id': result['delivery_id']},
                        start_to_close_timeout=SHORT_STAGE_TIMEOUT, retry_policy=DEFAULT_RETRY)
                else:
                    result = await workflow.execute_activity(deliver_activity, request,
                        start_to_close_timeout=SHORT_STAGE_TIMEOUT, retry_policy=DEFAULT_RETRY)
            except ActivityError as exc:
                if isinstance(exc.cause, ApplicationError) and exc.cause.non_retryable:
                    raise
                result = None
            if result and result.get('delivery_status') == 'success':
                return result
            self._business_status = 'delivery_exception' if result and result.get('delivery_status') == 'dead_letter' else 'pending_delivery'
            self._review_snapshot = {'job_id': request['job_id'], 'tenant_id': request['tenant_id'],
                                     'status': self._business_status, 'delivery': result}
            await workflow.sleep(min(interval, max(0, (deadline - workflow.now()).total_seconds())))
        return {**(result or {}), 'delivery_status': 'timed_out'}

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id", "unknown"))
        tenant_id = str(payload.get("tenant_id") or "default")
        def scoped(stage_payload: dict[str, Any]) -> dict[str, Any]:
            return {**stage_payload, "tenant_id": tenant_id}
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
                scoped(preprocess_payload),
                start_to_close_timeout=max(PIPELINE_TIMEOUT, timedelta(seconds=int(payload.get("preprocess_timeout_seconds", 300)))),
                retry_policy=DEFAULT_RETRY,
            )

            if preprocessed.get('pages') and workflow.patched('independent-pages-v1'):
                aggregated = await self._execute_pages(preprocessed, tenant_id, job_id)
                ocr, layout = aggregated['ocr'], aggregated['layout']
            else:
                ocr = await workflow.execute_activity(
                    ocr_activity,
                    scoped(build_ocr_payload(job_id, preprocessed)),
                    start_to_close_timeout=max(PIPELINE_TIMEOUT, timedelta(seconds=int(payload.get("ocr_timeout_seconds_per_page", 90)) * len(preprocessed.get("pages") or [1]) + 90)),
                    retry_policy=DEFAULT_RETRY,
                )

                layout = await workflow.execute_activity(
                    layout_activity,
                    scoped(build_layout_payload(job_id, preprocessed, ocr)),
                    start_to_close_timeout=max(PIPELINE_TIMEOUT, timedelta(seconds=int(payload.get("layout_timeout_seconds_per_page", 90)) * len(preprocessed.get("pages") or [1]) + 90)),
                    retry_policy=DEFAULT_RETRY,
                )

            classification = await workflow.execute_activity(
                classify_activity,
                scoped(build_classification_payload(job_id, ocr)),
                start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            extraction = await workflow.execute_activity(
                extract_activity,
                scoped(build_extraction_payload(job_id, ocr, layout, classification)),
                start_to_close_timeout=PIPELINE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            validation = await workflow.execute_activity(
                validate_activity,
                scoped(build_validation_payload(job_id, extraction)),
                start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                retry_policy=DEFAULT_RETRY,
            )

            if should_route_to_human_review(validation):
                review = await workflow.execute_activity(
                    create_review_task_activity,
                    build_review_payload(job_id, extraction, validation, tenant_id),
                    start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                    retry_policy=DEFAULT_RETRY,
                )
                self._business_status = "pending_human_review"
                branch_payload = {"review_task": review}
                self._review_snapshot = {
                    "job_id": job_id,
                    "status": "pending_human_review",
                    "classification": classification,
                    "preprocess": preprocessed,
                    "ocr": ocr,
                    "layout": layout,
                    "extraction": extraction,
                    "validation": validation,
                    "review_task": review,
                }
                review_task_id = str(review["review_task_id"])
                poll_interval = max(1, int(payload.get("review_poll_interval_seconds", 60)))
                wait_seconds = max(60, int(payload.get("review_wait_timeout_seconds", 604800)))
                deadline = workflow.now() + timedelta(seconds=wait_seconds)
                while workflow.now() < deadline:
                    decision = await workflow.execute_activity(
                        poll_review_activity,
                        {"job_id": job_id, "tenant_id": tenant_id, "review_task_id": review_task_id},
                        start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                        retry_policy=DEFAULT_RETRY,
                    )
                    if decision.get("status") == "decided":
                        if decision.get("job_id") != job_id or decision.get("tenant_id") != tenant_id or decision.get("review_task_id") != review_task_id:
                            raise PipelineContractError("review: decision identity mismatch")
                        branch_payload["review_decision"] = decision
                        if decision.get("decision") == "rejected":
                            final_status = "rejected"
                            break
                        if decision.get("decision") != "approved" or not isinstance(decision.get("fields"), dict):
                            raise PipelineContractError("review: invalid decision")
                        corrected_extraction = {**extraction, "fields": {**extraction["fields"], **decision["fields"]}}
                        validation = await workflow.execute_activity(
                            validate_activity,
                            {
                                **scoped(build_validation_payload(job_id, corrected_extraction)),
                                "human_review_approved": True,
                            },
                            start_to_close_timeout=SHORT_STAGE_TIMEOUT,
                            retry_policy=DEFAULT_RETRY,
                        )
                        if should_route_to_human_review(validation):
                            final_status = "review_validation_failed"
                            break
                        delivery_payload = scoped(build_delivery_payload(job_id, corrected_extraction, validation))
                        delivery_payload["metadata"] = {
                            **delivery_payload.get("metadata", {}),
                            "review_task_id": review_task_id,
                            "reviewer_id": decision.get("reviewer_id", ""),
                            "review_decision_key": decision.get("decision_key", ""),
                        }
                        delivery = await self._deliver_until_acknowledged(delivery_payload, payload)
                        extraction = corrected_extraction
                        branch_payload["delivery"] = delivery
                        final_status = "delivered" if delivery.get('delivery_status', 'success') == 'success' else 'delivery_timed_out'
                        break
                    remaining = (deadline - workflow.now()).total_seconds()
                    await workflow.sleep(min(poll_interval, remaining))
                else:
                    final_status = "review_timed_out"
            else:
                delivery = await self._deliver_until_acknowledged(scoped(build_delivery_payload(job_id, extraction, validation)), payload)
                final_status = "delivered" if delivery.get('delivery_status', 'success') == 'success' else 'delivery_timed_out'
                branch_payload = {"delivery": delivery}

            self._business_status = final_status
            self._review_snapshot = None

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
            webhook_event_type = "document.completed" if final_status == "delivered" else f"document.{final_status}"
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
            if workflow.patched("durable-ocr-exception-v1") and isinstance(exc, ActivityError):
                cause = exc.cause
                if isinstance(cause, ApplicationError) and (cause.type or "").startswith("OCR_"):
                    recorded = await workflow.execute_activity(
                        record_ocr_exception_activity,
                        {"tenant_id": tenant_id, "job_id": job_id, "failure_code": cause.type,
                         **({'page_number': self._current_page} if self._current_page is not None else {})},
                        start_to_close_timeout=SHORT_STAGE_TIMEOUT, retry_policy=DEFAULT_RETRY,
                    )
                    final_status = self._business_status = "ocr_exception"
                    return {"job_id": job_id, "tenant_id": tenant_id, "status": final_status,
                            "ocr_exception": recorded, "preprocess": preprocessed}
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
                    {**evaluation_payload, "tenant_id": tenant_id},
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
