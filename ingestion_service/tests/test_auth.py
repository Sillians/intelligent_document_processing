import asyncio
import unittest

from fastapi import HTTPException
from starlette.requests import Request

from ingestion_service.app.auth import resolve_principal
from shared.idp_common.config import Settings


class AuthorizationTests(unittest.TestCase):
    def principal(self, headers):
        request = Request({"type": "http", "method": "GET", "path": "/documents",
                           "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})
        return asyncio.run(resolve_principal(request, Settings(_env_file=None, ingestion_require_auth=True, ingestion_api_keys="key-a:tenant-a,key-b:tenant-b")))

    def test_client_actor_header_is_not_authenticated_identity(self):
        principal = self.principal({"x-api-key": "key-a", "x-actor-id": "administrator"})
        self.assertEqual(principal.subject, "api-key:tenant-a")
        self.assertEqual(principal.tenant_id, "tenant-a")

    def test_tenant_impersonation_is_denied(self):
        with self.assertRaises(HTTPException) as error:
            self.principal({"x-api-key": "key-a", "x-tenant-id": "tenant-b"})
        self.assertEqual(error.exception.status_code, 403)

    def test_invalid_key_is_denied(self):
        with self.assertRaises(HTTPException) as error:
            self.principal({"x-api-key": "invalid"})
        self.assertEqual(error.exception.status_code, 401)
