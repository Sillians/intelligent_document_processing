from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import httpx
from typing import Any, Protocol
from urllib.parse import urlparse

from delivery_service.app.models import DeliveryContext
from shared.idp_common.storage import upload_json


class DeliveryProvider(Protocol):
    name: str

    async def deliver(self, context: DeliveryContext) -> dict[str, Any]:
        ...


class ObjectStorageProvider:
    name = "object_storage"

    async def deliver(self, context: DeliveryContext) -> dict[str, Any]:
        key = f"jobs/{context.request.job_id}/delivery/{context.delivery_id}/payload.json"
        artifact = await asyncio.to_thread(
            upload_json,
            context.settings,
            context.settings.delivery_bucket,
            key,
            context.envelope,
        )
        return {
            "provider": self.name,
            "status": "success",
            "artifact": artifact,
            "bucket": context.settings.delivery_bucket,
            "key": key,
        }


class WebhookProvider:
    name = "webhook"

    async def deliver(self, context: DeliveryContext) -> dict[str, Any]:
        configured_url = str(getattr(context.settings, "delivery_webhook_url", "")).strip()
        url = context.request.webhook_url or configured_url
        if not url:
            raise ValueError("webhook destination requires webhook_url or DELIVERY_WEBHOOK_URL")
        if context.request.webhook_url:
            if not bool(getattr(context.settings, "delivery_allow_request_webhook_url", False)):
                raise ValueError("per-request webhook_url is disabled")
            allowed_hosts = {
                value.strip().lower()
                for value in str(getattr(context.settings, "delivery_webhook_allowed_hosts", "")).split(",")
                if value.strip()
            }
            hostname = (urlparse(url).hostname or "").lower()
            if not allowed_hosts or hostname not in allowed_hosts:
                raise ValueError("webhook_url host is not allowed")

        body = json.dumps(context.envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": context.delivery_id,
            "X-IDP-Delivery-Id": context.delivery_id,
            "X-IDP-Job-Id": context.request.job_id,
        }
        secret = str(getattr(context.settings, "delivery_webhook_secret", ""))
        if bool(getattr(context.settings, "delivery_webhook_require_signature", True)) and not secret:
            raise ValueError("webhook delivery requires DELIVERY_WEBHOOK_SECRET")
        if secret:
            signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-IDP-Signature-256"] = f"sha256={signature}"

        timeout = max(1, int(getattr(context.settings, "delivery_webhook_timeout_seconds", 15)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, content=body, headers=headers)
            response.raise_for_status()

        return {
            "provider": self.name,
            "status": "success",
            "status_code": response.status_code,
            "url": url,
        }


PROVIDERS: dict[str, type[ObjectStorageProvider] | type[WebhookProvider]] = {
    ObjectStorageProvider.name: ObjectStorageProvider,
    WebhookProvider.name: WebhookProvider,
}


def build_provider(name: str) -> DeliveryProvider:
    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ValueError(f"unsupported delivery provider: {name}")
    return provider_class()
