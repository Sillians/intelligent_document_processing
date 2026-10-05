from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient

from ingestion_service.app.main import app, get_repository
from ingestion_service.app.repository import IngestionRepository
from ingestion_service.app.reprocessing import reconcile_reprocess_results, request_fingerprint
from shared.idp_common.config import get_settings
from shared.idp_common.operator_auth import Operator
import hashlib


class ReprocessingTests(unittest.TestCase):
    def test_source_expired_during_precheck_cannot_register_child(self):
        repository = object.__new__(IngestionRepository)
        cursor = Mock()
        cursor.fetchone.side_effect = [{'artifact_bucket': None, 'artifact_key': None}, None]
        context = MagicMock()
        context.__enter__.return_value = (Mock(), cursor)
        with patch.object(repository, '_cursor', return_value=context):
            with self.assertRaisesRegex(ValueError, 'expired'):
                repository.create_reprocess_job('tenant-a', 'parent', 'child', 'key', 'fingerprint', 'operator', 'Engine restored')
        self.assertEqual(cursor.execute.call_count, 2)

    def setUp(self):
        self.settings = get_settings()
        self.key_patch = patch.object(self.settings, 'idp_operator_api_keys', 'operator-a:tenant-a,operator-b:tenant-b')
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.repo = Mock()
        self.repo.get_reprocess_request.return_value = None
        self.repo.get_job_by_id.return_value = {'job_id': 'parent', 'tenant_id': 'tenant-a', 'workflow_id': 'workflow-parent',
            'artifact_bucket': self.settings.raw_bucket, 'artifact_key': 'tenants/tenant-a/jobs/parent/raw/file.png', 'sha256': 'hash', 'size_bytes': 10}
        app.dependency_overrides[get_repository] = lambda: self.repo
        self.addCleanup(app.dependency_overrides.clear)
        temporal = Mock()
        handle = temporal.get_workflow_handle.return_value
        handle.describe = AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name='COMPLETED')))
        handle.result = AsyncMock(return_value={'status': 'ocr_exception'})
        patcher = patch.object(app.state, 'temporal_client', temporal, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.headers = {'X-IDP-Operator-Key': 'operator-a', 'Idempotency-Key': 'resolution-key'}

    def test_intake_key_is_not_an_operator_credential(self):
        response = self.client.post('/exceptions/parent/reprocess', headers={'X-API-Key': 'intake-key', 'Idempotency-Key': 'resolution-key'}, json={'reason': 'Engine restored'})
        self.assertEqual(response.status_code, 401)

    def test_operator_cannot_impersonate_tenant(self):
        response = self.client.post('/exceptions/parent/reprocess', headers={**self.headers, 'X-Tenant-Id': 'tenant-b'}, json={'reason': 'Engine restored'})
        self.assertEqual(response.status_code, 403)

    @patch('ingestion_service.app.reprocessing.fetch_exceptions', new_callable=AsyncMock)
    def test_temporal_outage_does_not_register_reprocessing(self, fetch):
        app.state.temporal_client.get_workflow_handle.return_value.describe.side_effect = ConnectionError('Temporal offline')
        response = self.client.post('/exceptions/parent/reprocess', headers=self.headers, json={'reason': 'Engine restored'})
        self.assertEqual(response.status_code, 503)
        self.repo.create_reprocess_job.assert_not_called()

    @patch('ingestion_service.app.reprocessing.fetch_exceptions', new_callable=AsyncMock)
    @patch('ingestion_service.app.reprocessing.download_verified_bytes', side_effect=ValueError('bad checksum'))
    def test_bad_source_creates_no_resolution(self, download, fetch):
        response = self.client.post('/exceptions/parent/reprocess', headers=self.headers, json={'reason': 'Engine restored'})
        self.assertEqual(response.status_code, 409)
        self.repo.create_reprocess_job.assert_not_called()

    @patch('ingestion_service.app.reprocessing.fetch_exceptions', new_callable=AsyncMock)
    @patch('ingestion_service.app.reprocessing.download_verified_bytes', side_effect=ConnectionError('storage offline'))
    def test_transient_source_failure_stays_unresolved(self, download, fetch):
        response = self.client.post('/exceptions/parent/reprocess', headers=self.headers, json={'reason': 'Engine restored'})
        self.assertEqual(response.status_code, 503)
        self.repo.create_reprocess_job.assert_not_called()

    def test_immutable_resolution_replay_does_not_require_source_read(self):
        operator = Operator('tenant-a', 'operator:' + hashlib.sha256(b'operator-a').hexdigest()[:16])
        self.repo.get_reprocess_request.return_value = {'child_job_id': 'child', 'request_key': 'resolution-key',
            'fingerprint': request_fingerprint('Engine restored', operator), 'status': 'requested'}
        response = self.client.post('/exceptions/parent/reprocess', headers=self.headers, json={'reason': 'Engine restored'})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()['idempotent_replay'])
        self.repo.create_reprocess_job.assert_not_called()
        changed = self.client.post('/exceptions/parent/reprocess', headers=self.headers, json={'reason': 'Different reason'})
        self.assertEqual(changed.status_code, 409)


class ResolutionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_workflow_read_does_not_starve_other_records(self):
        repository = Mock()
        repository.list_reprocess_inflight.return_value = [
            {'tenant_id': 'tenant-a', 'parent_job_id': 'parent-1', 'child_job_id': 'child-1'},
            {'tenant_id': 'tenant-a', 'parent_job_id': 'parent-2', 'child_job_id': 'child-2'},
        ]
        with patch('ingestion_service.app.reprocessing.reconcile_reprocess_result', new_callable=AsyncMock) as reconcile, \
             patch('ingestion_service.app.reprocessing.logging.getLogger'):
            reconcile.side_effect = [ConnectionError('Temporal unavailable'), None]
            await reconcile_reprocess_results(repository, Mock())
        self.assertEqual(reconcile.await_count, 2)
        self.assertEqual(repository.record_reprocess_check.call_count, 2)
