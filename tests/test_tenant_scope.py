import unittest

from fastapi import HTTPException

from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope


class TenantScopeTests(unittest.TestCase):
    def test_non_default_tenant_gets_scoped_artifact(self) -> None:
        key = artifact_key("tenant-a", "job-1", "ocr/ocr.json")
        self.assertEqual(key, "tenants/tenant-a/jobs/job-1/ocr/ocr.json")
        require_artifact_scope("tenant-a", "job-1", key)

    def test_cross_tenant_and_cross_job_keys_are_denied(self) -> None:
        with self.assertRaises(HTTPException):
            require_artifact_scope("tenant-b", "job-1", artifact_key("tenant-a", "job-1", "ocr/ocr.json"))
        with self.assertRaises(HTTPException):
            require_artifact_scope("tenant-a", "job-2", artifact_key("tenant-a", "job-1", "ocr/ocr.json"))

    def test_invalid_tenant_identifier_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            artifact_key("../escape", "job-1", "raw/doc.png")

    def test_ambiguous_or_traversing_object_paths_are_denied(self) -> None:
        prefix = "tenants/tenant-a/jobs/job-1/"
        for suffix in ("../other", "raw//doc.png", "raw/./doc.png", "raw/%2e%2e/doc.png", "raw\\doc.png"):
            with self.subTest(suffix=suffix), self.assertRaises(HTTPException):
                require_artifact_scope("tenant-a", "job-1", prefix + suffix)
