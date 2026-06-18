from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from clients.python.idp_client import IdpClient, _multipart_body
from ingestion_service.app.main import _error_response, api_root


def request_with_headers(headers: dict[str, str] | None = None) -> Request:
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    return Request({"type": "http", "method": "GET", "path": "/documents", "headers": raw_headers})


class PublicApiContractTests(unittest.TestCase):
    def test_api_root_documents_gateway_usage(self) -> None:
        payload = asyncio.run(api_root())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["endpoints"]["health"], "/health")
        self.assertEqual(payload["endpoints"]["submit_document"], "POST /documents")
        self.assertEqual(payload["endpoints"]["poll_status"], "GET /documents/{job_id}")
        self.assertEqual(payload["endpoints"]["fetch_result"], "GET /documents/{job_id}/result")

    def test_error_response_uses_stable_envelope_and_request_id(self) -> None:
        request = request_with_headers({"X-Request-ID": "req-123"})

        response = _error_response(
            request=request,
            status_code=401,
            detail="Missing API credentials",
            default_message="Request failed",
        )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["x-request-id"], "req-123")
        self.assertEqual(payload["error"]["code"], "missing_api_credentials")
        self.assertEqual(payload["error"]["message"], "Missing API credentials")
        self.assertEqual(payload["error"]["status"], 401)
        self.assertEqual(payload["error"]["request_id"], "req-123")

    def test_error_response_preserves_validation_details(self) -> None:
        request = request_with_headers()
        details = [{"loc": ["body", "file"], "msg": "Field required", "type": "missing"}]

        response = _error_response(
            request=request,
            status_code=422,
            detail=details,
            default_message="Request validation failed",
        )

        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["code"], "validation_error")
        self.assertEqual(payload["error"]["details"], details)
        self.assertTrue(payload["error"]["request_id"])

    def test_client_headers_include_auth_tenant_actor_request_and_idempotency(self) -> None:
        client = IdpClient("http://localhost:8081", "key-1", "tenant-a", "actor-a")

        headers = client._headers(idempotency_key="idem-1")

        self.assertEqual(headers["X-API-Key"], "key-1")
        self.assertEqual(headers["X-Tenant-Id"], "tenant-a")
        self.assertEqual(headers["X-Actor-Id"], "actor-a")
        self.assertEqual(headers["Idempotency-Key"], "idem-1")
        self.assertTrue(headers["X-Request-ID"])

    def test_multipart_body_contains_file_and_client_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invoice.txt"
            path.write_text("hello", encoding="utf-8")

            body, content_type = _multipart_body(path, source="api", external_reference="invoice-1")

        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="source"', body)
        self.assertIn(b"api", body)
        self.assertIn(b'name="external_reference"', body)
        self.assertIn(b"invoice-1", body)
        self.assertIn(b'name="file"; filename="invoice.txt"', body)
        self.assertIn(b"hello", body)


if __name__ == "__main__":
    unittest.main()
