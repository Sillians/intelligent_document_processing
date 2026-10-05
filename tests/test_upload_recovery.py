import asyncio
import hashlib
import unittest
from unittest.mock import Mock, patch
from ingestion_service.app.main import _reconcile_uploads
from shared.idp_common.config import Settings
from shared.idp_common.storage import verify_artifact


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.job = {"job_id": "job-1", "tenant_id": "tenant-a", "original_filename": "doc.png", "sha256": "hash", "size_bytes": 10, "age_seconds": 180}

    def test_matching_artifact_is_registered_for_dispatch(self):
        repository = Mock()
        repository.list_incomplete_uploads.return_value = [self.job]
        with patch("ingestion_service.app.main.object_exists", return_value=True), patch("ingestion_service.app.main.verify_artifact") as verify:
            asyncio.run(_reconcile_uploads(repository, Settings(_env_file=None)))
        verify.assert_called_once()
        repository.update_artifact.assert_called_once()
        repository.mark_failed.assert_not_called()

    def test_missing_upload_waits_then_fails_after_deadline(self):
        for age, failed in ((180, False), (901, True)):
            with self.subTest(age=age):
                repository = Mock()
                repository.list_incomplete_uploads.return_value = [{**self.job, "age_seconds": age}]
                with patch("ingestion_service.app.main.object_exists", return_value=False):
                    asyncio.run(_reconcile_uploads(repository, Settings(_env_file=None)))
                self.assertEqual(repository.mark_failed.called, failed)
                repository.update_artifact.assert_not_called()

    def test_corrupt_artifact_never_dispatches(self):
        repository = Mock()
        repository.list_incomplete_uploads.return_value = [{"job_id": "job-1", "tenant_id": "tenant-a", "original_filename": "doc.png", "sha256": "hash", "size_bytes": 10, "age_seconds": 180}]
        with patch("ingestion_service.app.main.object_exists", return_value=True), patch("ingestion_service.app.main.verify_artifact", side_effect=ValueError("checksum mismatch")):
            asyncio.run(_reconcile_uploads(repository, Settings(_env_file=None)))
        repository.update_artifact.assert_not_called()
        self.assertEqual(repository.mark_failed.call_args.args[1], "RAW_ARTIFACT_CORRUPT")

    def test_transient_storage_failure_preserves_received_state(self):
        repository = Mock()
        repository.list_incomplete_uploads.return_value = [{"job_id": "job-1", "tenant_id": "tenant-a", "original_filename": "doc.png", "sha256": "hash", "size_bytes": 10, "age_seconds": 180}]
        with patch("ingestion_service.app.main.object_exists", return_value=True), patch("ingestion_service.app.main.verify_artifact", side_effect=ConnectionError("storage unavailable")):
            with self.assertRaises(ConnectionError):
                asyncio.run(_reconcile_uploads(repository, Settings(_env_file=None)))
        repository.mark_failed.assert_not_called()

    def test_checksum_and_length_validation_close_the_storage_stream(self):
        data = b"verified-document"
        for digest, size, valid in ((hashlib.sha256(data).hexdigest(), len(data), True), ("wrong", len(data), False), ("wrong", 100, False)):
            with self.subTest(size=size, valid=valid):
                body = Mock()
                body.iter_chunks.return_value = [data[:3], data[3:]]
                client = Mock()
                client.get_object.return_value = {"ContentLength": len(data), "Body": body}
                with patch("shared.idp_common.storage.get_s3_client", return_value=client):
                    if valid:
                        verify_artifact(Settings(_env_file=None), "raw", "key", digest, size)
                    else:
                        with self.assertRaises(ValueError):
                            verify_artifact(Settings(_env_file=None), "raw", "key", digest, size)
                body.close.assert_called_once()
