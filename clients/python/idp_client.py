#!/usr/bin/env python3
"""Small dependency-free client for the public IDP API."""

from __future__ import annotations

import argparse
import json
import mimetypes
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


TERMINAL_WORKFLOW_STATUSES = {"COMPLETED", "FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"}


class IdpApiError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.payload = payload
        error_payload = payload.get("error") if isinstance(payload, dict) else None
        message = error_payload.get("message") if isinstance(error_payload, dict) else str(payload)
        super().__init__(f"IDP API HTTP {status}: {message}")


@dataclass(frozen=True)
class IdpClient:
    base_url: str
    api_key: str
    tenant_id: str
    actor_id: str = "idp-client"
    timeout_seconds: int = 60

    def submit_document(
        self,
        path: Path,
        *,
        idempotency_key: str | None = None,
        external_reference: str | None = None,
        source: str = "api",
    ) -> dict[str, Any]:
        body, content_type = _multipart_body(path, source=source, external_reference=external_reference)
        headers = self._headers(idempotency_key=idempotency_key or f"{path.name}:{uuid.uuid4()}")
        headers["Content-Type"] = content_type
        return self._json("POST", "/documents", headers=headers, body=body)

    def get_status(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/documents/{job_id}", headers=self._headers())

    def get_result(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/documents/{job_id}/result", headers=self._headers())

    def wait_for_completion(self, job_id: str, *, timeout_seconds: int = 600, poll_interval_seconds: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_status = self.get_status(job_id)
            status = str(last_status.get("workflow_status") or last_status.get("status") or "UNKNOWN")
            print(f"workflow status: {status}", flush=True)
            if status in TERMINAL_WORKFLOW_STATUSES:
                return last_status
            time.sleep(poll_interval_seconds)
        raise TimeoutError(f"Timed out waiting for IDP job {job_id}")

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "X-API-Key": self.api_key,
            "X-Tenant-Id": self.tenant_id,
            "X-Actor-Id": self.actor_id,
            "X-Request-ID": str(uuid.uuid4()),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _json(self, method: str, path: str, *, headers: dict[str, str], body: bytes | None = None) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except error.HTTPError as exc:
            payload = _decode_json(exc.read())
            if not isinstance(payload, dict):
                payload = {"error": {"message": str(payload), "status": exc.code}}
            raise IdpApiError(exc.code, payload) from exc

        data = _decode_json(payload)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object from {url}")
        return data


def _decode_json(payload: bytes) -> Any:
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def _multipart_body(path: Path, *, source: str, external_reference: str | None) -> tuple[bytes, str]:
    boundary = f"----idp-client-{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )

    add_field("source", source)
    if external_reference:
        add_field("external_reference", external_reference)

    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode(),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit and poll a document through the public IDP API")
    parser.add_argument("document", type=Path)
    parser.add_argument("--base-url", default="http://localhost:8081")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--actor-id", default="idp-client")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--external-reference")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args()

    client = IdpClient(args.base_url, args.api_key, args.tenant_id, args.actor_id)
    submission = client.submit_document(
        args.document,
        idempotency_key=args.idempotency_key,
        external_reference=args.external_reference,
    )
    print(json.dumps({"submission": submission}, indent=2, sort_keys=True))
    status = client.wait_for_completion(
        str(submission["job_id"]),
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval,
    )
    output = {"final_status": status}
    if str(status.get("workflow_status")) == "COMPLETED":
        output["result"] = client.get_result(str(submission["job_id"]))
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
