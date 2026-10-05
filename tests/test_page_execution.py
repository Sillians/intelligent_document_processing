from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from shared.idp_common.config import get_settings
from shared.idp_common.tenant_scope import artifact_key
from workflow_orchestrator.app.page_execution import aggregate_pages
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow


class PageExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_pages_are_rejected_before_any_activity(self):
        payload, _ = self.fixture()
        payload['pages'][1]['preprocessed_key'] = payload['pages'][0]['preprocessed_key']
        with patch('workflow_orchestrator.app.workflows.workflow.execute_activity', new_callable=AsyncMock) as execute:
            from workflow_orchestrator.app.pipeline import PipelineContractError
            with self.assertRaises(PipelineContractError):
                await DocumentPipelineWorkflow()._execute_pages(payload, 'a', 'j')
            execute.assert_not_called()

    async def test_foreign_page_is_rejected_before_any_activity(self):
        payload, _ = self.fixture()
        payload['pages'][1]['preprocessed_key'] = artifact_key('other', 'j', 'preprocessed/page-0002.png')
        with patch('workflow_orchestrator.app.workflows.workflow.execute_activity', new_callable=AsyncMock) as execute:
            from workflow_orchestrator.app.pipeline import PipelineContractError
            with self.assertRaises(PipelineContractError):
                await DocumentPipelineWorkflow()._execute_pages(payload, 'a', 'j')
            execute.assert_not_called()

    def fixture(self):
        settings = get_settings()
        payload = {'tenant_id': 'a', 'job_id': 'j', 'pages': [], 'results': []}
        documents = {}
        for number in (1, 2):
            source = artifact_key('a', 'j', f'preprocessed/page-{number:04d}.png')
            payload['pages'].append({'page_number': number, 'preprocessed_key': source})
            result = {'page_number': number}
            for stage, bucket in [('ocr', settings.ocr_bucket), ('layout', settings.layout_bucket)]:
                key = artifact_key('a', 'j', f'{stage}/pages/{number:04d}.json')
                result[stage] = {stage + '_key': key, stage + '_bucket': bucket}
                page = {'page_number': 1, 'preprocessed_key': source, 'full_text': str(number)}
                if stage == 'ocr':
                    page['tokens'] = [{'text': str(number), 'confidence': 0.9, 'bbox': [0, 0, 10, 10]}]
                else:
                    page.update(blocks=[{'id': 'p0001-b1', 'text': str(number)}], image_width=10, image_height=10)
                documents[key] = {'schema_version': '2.0', 'tenant_id': 'a', 'job_id': 'j', 'page_slot': number, 'pages': [page]}
            payload['results'].append(result)
        return payload, documents

    def test_aggregation_preserves_order_coordinates_and_unique_blocks(self):
        payload, documents = self.fixture()
        writes = []
        with patch('workflow_orchestrator.app.page_execution.download_json', side_effect=lambda s, b, k: deepcopy(documents[k])), \
             patch('workflow_orchestrator.app.page_execution.upload_json', side_effect=lambda s,b,k,d: writes.append(deepcopy(d))):
            result = aggregate_pages(payload)
        self.assertEqual(result['ocr']['full_text'], '1\n2')
        self.assertEqual(result['ocr']['page_count'], 2)
        self.assertEqual([t['page_number'] for t in writes[0]['tokens']], [1, 2])
        self.assertEqual(writes[0]['tokens'][0]['bbox'], [0, 0, 10, 10])
        self.assertEqual(len({b['id'] for b in writes[1]['blocks']}), 2)

    def test_foreign_or_incomplete_artifacts_never_publish_document(self):
        payload, documents = self.fixture()
        key = payload['results'][1]['ocr']['ocr_key']
        documents[key]['tenant_id'] = 'foreign'
        with patch('workflow_orchestrator.app.page_execution.download_json', side_effect=lambda s,b,k: documents[k]), \
             patch('workflow_orchestrator.app.page_execution.upload_json') as upload:
            with self.assertRaises(ValueError):
                aggregate_pages(payload)
            upload.assert_not_called()

    async def test_page_stages_have_independent_stable_activity_ids(self):
        payload, _ = self.fixture()
        calls = []
        async def execute(fn, request, **kwargs):
            calls.append((fn.__name__, request, kwargs))
            if fn.__name__ == 'ocr_page_activity':
                return {'ocr_key': 'page-result'}
            return {}
        with patch('workflow_orchestrator.app.workflows.workflow.execute_activity', new=execute):
            await DocumentPipelineWorkflow()._execute_pages({'pages':payload['pages']}, 'a', 'j')
        self.assertEqual([kwargs.get('activity_id') for _,_,kwargs in calls[:4]],
                         ['ocr-page-0001', 'layout-page-0001', 'ocr-page-0002', 'layout-page-0002'])
        self.assertEqual(calls[-1][0], 'aggregate_pages_activity')
