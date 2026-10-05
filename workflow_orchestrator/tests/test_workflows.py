from __future__ import annotations

import unittest
from temporalio.exceptions import ActivityError, ApplicationError
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow


class ReviewWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_wait_expires_without_false_success(self):
        instance = DocumentPipelineWorkflow()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = Mock(side_effect=[start, start, start, start + timedelta(seconds=61)])
        execute = AsyncMock(return_value={'delivery_id': 'delivery-1', 'delivery_status': 'pending'})
        with patch('workflow_orchestrator.app.workflows.workflow.patched', return_value=True), \
             patch('workflow_orchestrator.app.workflows.workflow.now', new=now), \
             patch('workflow_orchestrator.app.workflows.workflow.execute_activity', new=execute), \
             patch('workflow_orchestrator.app.workflows.workflow.sleep', new=AsyncMock()):
            result = await instance._deliver_until_acknowledged({'job_id':'job-1','tenant_id':'tenant-a'}, {'delivery_wait_timeout_seconds':60})
        self.assertEqual(result['delivery_status'], 'timed_out')
        self.assertEqual(instance.business_status(), 'pending_delivery')

    async def test_pre_patch_history_preserves_one_delivery_command(self):
        instance = DocumentPipelineWorkflow()
        execute = AsyncMock(return_value={'delivery_status': 'success'})
        with patch('workflow_orchestrator.app.workflows.workflow.patched', return_value=False), \
             patch('workflow_orchestrator.app.workflows.workflow.execute_activity', new=execute):
            await instance._deliver_until_acknowledged({'job_id':'job-1','tenant_id':'tenant-a'}, {})
        self.assertEqual(execute.await_count, 1)
        self.assertEqual(execute.call_args.args[0].__name__, 'deliver_activity')

    async def test_queued_and_dead_letter_delivery_are_not_treated_as_delivered(self):
        instance = DocumentPipelineWorkflow()
        states = iter(['pending', 'dead_letter', 'success'])
        calls = []
        async def execute(activity, payload, **kwargs):
            calls.append(activity.__name__)
            return {'delivery_id': 'delivery-1', 'delivery_status': next(states)}
        sleep = AsyncMock()
        with patch('workflow_orchestrator.app.workflows.workflow.patched', return_value=True), \
             patch('workflow_orchestrator.app.workflows.workflow.now', return_value=datetime(2026,1,1,tzinfo=timezone.utc)), \
             patch('workflow_orchestrator.app.workflows.workflow.execute_activity', new=execute), \
             patch('workflow_orchestrator.app.workflows.workflow.sleep', new=sleep):
            result = await instance._deliver_until_acknowledged({'job_id':'job-1','tenant_id':'tenant-a'}, {})
        self.assertEqual(result['delivery_status'], 'success')
        self.assertEqual(calls, ['deliver_activity','poll_delivery_activity','poll_delivery_activity'])
        self.assertEqual(sleep.await_count, 2)

    async def _run_review(self, decision: str, *, invalid_correction=False, expired=False, ocr_failed=False):
        calls: list[tuple[str, dict]] = []
        responses = {
            "preprocess_activity": {"preprocessed_key": "tenants/tenant-a/jobs/job-1/preprocess/page.png"},
            "ocr_activity": {"ocr_key": "tenants/tenant-a/jobs/job-1/ocr/result.json", "mean_confidence": 0.65},
            "layout_activity": {"layout_key": "tenants/tenant-a/jobs/job-1/layout/result.json"},
            "classify_activity": {"route": "invoice"},
            "extract_activity": {
                "extraction_key": "tenants/tenant-a/jobs/job-1/extraction/result.json",
                "fields": {"invoice_number": "INV-1", "total_amount": ""},
                "confidence": 0.65,
                "used_vlm_fallback": False,
            },
            "create_review_task_activity": {"review_task_id": "review-1", "review_status": "queued_in_label_studio"},
            "poll_review_activity": {
                "status": "decided", "job_id": "job-1", "tenant_id": "tenant-a",
                "review_task_id": "review-1", "decision": decision,
                "fields": {"total_amount": "42.00"}, "reviewer_id": "analyst-1",
            },
            "deliver_activity": {"delivery_status": "success", "delivery_id": "delivery-1"},
            "notify_webhook_activity": {},
            "evaluate_activity": {},
        }

        async def execute_activity(activity, payload, **_kwargs):
            name = activity.__name__
            calls.append((name, payload))
            if name == "ocr_activity" and ocr_failed:
                error = ActivityError("OCR retries exhausted", scheduled_event_id=1, started_event_id=2,
                                      identity="test", activity_type="ocr_activity", activity_id="ocr", retry_state=None)
                error.__cause__ = ApplicationError("OCR_TIMEOUT", type="OCR_TIMEOUT")
                raise error
            if name == "record_ocr_exception_activity":
                return {**payload, "status": "ocr_exception"}
            if name == "validate_activity":
                reviewed = payload.get("human_review_approved", False)
                return {"verdict": "human_approved" if reviewed and not invalid_correction else "needs_review", "requires_human_review": not reviewed or invalid_correction}
            return responses[name]

        instance = DocumentPipelineWorkflow()
        with (
            patch("workflow_orchestrator.app.workflows.workflow.patched", return_value=True),
            patch("workflow_orchestrator.app.workflows.workflow.execute_activity", new=execute_activity),
            patch("workflow_orchestrator.app.workflows.workflow.info", return_value=SimpleNamespace(workflow_id="idp-tenant-a-job-1")),
            patch("workflow_orchestrator.app.workflows.workflow.now", side_effect=([datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)] if expired else None), return_value=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            patch("workflow_orchestrator.app.workflows.workflow.logger", new=Mock()),
        ):
            result = await instance.run({
                "job_id": "job-1", "tenant_id": "tenant-a", "raw_bucket": "raw-documents",
                "raw_key": "tenants/tenant-a/jobs/job-1/raw/invoice.png",
            })
        return result, calls, instance

    async def test_approval_revalidates_corrected_fields_before_delivery(self) -> None:
        result, calls, instance = await self._run_review("approved")
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["extraction"]["fields"]["total_amount"], "42.00")
        self.assertEqual(instance.business_status(), "delivered")
        self.assertIsNone(instance.review_snapshot())
        revalidation = [payload for name, payload in calls if name == "validate_activity"][-1]
        self.assertTrue(revalidation["human_review_approved"])
        self.assertEqual(revalidation["fields"]["total_amount"], "42.00")
        delivered = next(payload for name, payload in calls if name == "deliver_activity")
        self.assertEqual(delivered["payload"]["total_amount"], "42.00")
        self.assertEqual(delivered["approval_status"], "human_approved")
        self.assertEqual(delivered["tenant_id"], "tenant-a")

    async def test_rejection_never_delivers(self) -> None:
        result, calls, _instance = await self._run_review("rejected")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("deliver_activity", [name for name, _payload in calls])

    async def test_invalid_correction_never_delivers(self):
        result, calls, _ = await self._run_review("approved", invalid_correction=True)
        self.assertEqual(result["status"], "review_validation_failed")
        self.assertNotIn("deliver_activity", [name for name, _ in calls])

    async def test_timeout_never_delivers(self):
        result, calls, _ = await self._run_review("approved", expired=True)
        self.assertEqual(result["status"], "review_timed_out")
        self.assertNotIn("deliver_activity", [name for name, _ in calls])

    async def test_exhausted_ocr_persists_exception_and_never_delivers(self):
        result, calls, instance = await self._run_review("approved", ocr_failed=True)
        self.assertEqual(result["status"], "ocr_exception")
        self.assertEqual(instance.business_status(), "ocr_exception")
        recorded = [payload for name, payload in calls if name == "record_ocr_exception_activity"]
        self.assertEqual(recorded[0]["failure_code"], "OCR_TIMEOUT")
        self.assertNotIn("deliver_activity", [name for name, _ in calls])
