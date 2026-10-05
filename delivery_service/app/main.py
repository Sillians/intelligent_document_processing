from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
import httpx
from prometheus_client import Gauge
from pydantic import BaseModel, Field

from delivery_service.app.models import DeliveryContext, DeliveryRequest, WebhookEventRequest
from delivery_service.app.providers import build_provider
from delivery_service.app.repository import DeliveryRepository
from shared.idp_common.config import get_settings
from shared.idp_common.internal_auth import install_internal_auth
from shared.idp_common.metrics import instrument_app
from shared.idp_common.operator_auth import Operator, resolve_operator
from shared.idp_common.storage import download_json, object_exists, upload_json
from shared.idp_common.tenant_scope import artifact_key, require_artifact_scope

settings = get_settings()
logger = logging.getLogger("delivery_service")
_DELIVERY_SEMAPHORE = asyncio.Semaphore(settings.delivery_max_inflight_requests)
PENDING = Gauge("idp_delivery_pending_destinations", "Pending and leased delivery work")
DEAD_LETTERS = Gauge("idp_delivery_dead_letter_destinations", "Delivery work requiring redrive")
OLDEST = Gauge("idp_delivery_oldest_unfinished_age_seconds", "Age of oldest unfinished delivery")

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.repository = await asyncio.to_thread(DeliveryRepository, settings)
    task = asyncio.create_task(_reconcile(), name="delivery-reconciler")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(app.state.repository.close)

app = FastAPI(title="delivery-service", lifespan=lifespan)
instrument_app(app, "delivery-service")
install_internal_auth(app)

def get_repository():
    repository = getattr(app.state, "repository", None)
    if repository is None:
        raise HTTPException(503, "Delivery ledger unavailable")
    return repository

def _stable_json(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        raise HTTPException(422, "Delivery must contain finite JSON values") from exc

def _provider_names(request):
    names = request.destinations if request.destinations is not None else settings.delivery_providers.split(",")
    return list(dict.fromkeys(str(name).strip().lower() for name in names if str(name).strip())) or ["object_storage"]

def _delivery_id(request, providers):
    source = request.idempotency_key.strip() if request.idempotency_key else _stable_json({**request.model_dump(mode="json"), "destinations": providers})
    return "delivery-" + hashlib.sha256(source.encode()).hexdigest()[:24]

def _redact_payload(payload):
    fields = {value.strip() for value in settings.delivery_redact_fields.split(",") if value.strip()}
    redacted = set()
    def visit(value):
        if isinstance(value, dict):
            output = {}
            for key, nested in value.items():
                if key in fields:
                    output[key] = "[REDACTED]"
                    redacted.add(key)
                else:
                    output[key] = visit(nested)
            return output
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value
    return visit(payload), sorted(redacted)

def _validate_approval(request):
    allowed = {value.strip().lower() for value in settings.delivery_allowed_approval_statuses.split(",")}
    if settings.delivery_require_approval and (request.approval_status or "").lower() not in allowed:
        raise HTTPException(409, "Delivery requires an approved validation verdict")

def _webhook_url(request):
    url = request.webhook_url or settings.delivery_webhook_url or None
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(422, "Webhook URL must be absolute http(s), without credentials or fragments")
    if request.webhook_url:
        allowed = {name.strip().lower() for name in settings.delivery_webhook_allowed_hosts.split(",") if name.strip()}
        if not settings.delivery_allow_request_webhook_url or (parsed.hostname or "").lower() not in allowed:
            raise HTTPException(422, "Per-request webhook destination is not allowed")
    if settings.delivery_webhook_require_signature and not settings.delivery_webhook_secret:
        raise HTTPException(422, "Webhook delivery requires DELIVERY_WEBHOOK_SECRET")
    return url

def _webhook_event_id(request):
    if request.event_id:
        return request.event_id.strip()
    source = request.idempotency_key.strip() if request.idempotency_key else _stable_json(request.model_dump(mode="json"))
    return "evt-" + hashlib.sha256(source.encode()).hexdigest()[:24]

def _webhook_envelope(request, event_id):
    return {"schema_version": "1.0", "event_id": event_id, "event_type": request.event_type,
            "occurred_at": request.occurred_at or datetime.now(UTC).isoformat(),
            "tenant_id": request.tenant_id, "job_id": request.job_id, "workflow_id": request.workflow_id, "data": request.data}

def _webhook_headers(event_id, envelope, body):
    headers = {"Content-Type": "application/json", "Idempotency-Key": event_id,
               "X-IDP-Event-Id": event_id, "X-IDP-Event-Type": envelope["event_type"],
               "X-IDP-Job-Id": envelope["job_id"], "X-IDP-Tenant-Id": envelope["tenant_id"]}
    secret = settings.delivery_webhook_secret
    if settings.delivery_webhook_require_signature and not secret:
        raise ValueError("Webhook delivery requires DELIVERY_WEBHOOK_SECRET")
    if secret:
        headers["X-IDP-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return headers

async def _send_webhook_event(url, event_id, envelope):
    body = _stable_json(envelope).encode()
    async with httpx.AsyncClient(timeout=settings.delivery_webhook_timeout_seconds, follow_redirects=False) as client:
        response = await client.post(url, content=body, headers=_webhook_headers(event_id, envelope, body))
    response.raise_for_status()
    return {"provider": "webhook", "status": "success", "status_code": response.status_code, "url": url}

def _receipt_key(job, operation, tenant="default"):
    return artifact_key(tenant, job, f"delivery/{operation}/receipt.json")

def _webhook_receipt_key(job, operation, tenant="default"):
    return artifact_key(tenant, job, f"webhooks/{operation}/receipt.json")

def _legacy_receipt(tenant, job, operation, kind):
    key = (_receipt_key if kind == "delivery" else _webhook_receipt_key)(job, operation, tenant)
    for bucket in (settings.delivery_bucket, settings.validation_bucket):
        if object_exists(settings, bucket, key):
            return download_json(settings, bucket, key)
    return None

def operation_response(operation, replay=False):
    receipt = next((work["result"] for work in operation["work"] if work["provider"] == "__receipt__" and work["state"] == "acknowledged"), None)
    if receipt:
        response = {**receipt, "idempotent_replay": replay}
        storage = next((result for result in receipt.get("destination_results", []) if result.get("provider") == "object_storage"), {})
        response["delivery_artifact"] = storage.get("artifact", "")
        return response
    return {"job_id": operation["job_id"], "tenant_id": operation["tenant_id"], "delivery_id": operation["operation_id"],
            "event_id": operation["operation_id"] if operation["kind"] == "event" else None,
            "delivery_status": operation["state"], "webhook_status": operation["state"],
            "destination_results": [work["result"] for work in operation["work"] if work["provider"] != "__receipt__" and work["state"] == "acknowledged"],
            "work": [{key: work[key] for key in ("provider", "state", "attempt_count", "last_error")} for work in operation["work"]],
            "idempotent_replay": replay}

def _project_receipt(operation):
    results = [work["result"] for work in operation["work"] if work["provider"] != "__receipt__"]
    receipt = {"tenant_id": operation["tenant_id"], "job_id": operation["job_id"], "destination_results": results,
               "providers": operation["providers"],
               "delivered_at": max([result["acknowledged_at"] for result in results] or [operation["created_at"].isoformat()])}
    if operation["kind"] == "event":
        receipt.update(event_id=operation["operation_id"], event_type=operation["envelope"]["event_type"],
                       webhook_status="success" if results else "skipped", envelope=operation["envelope"])
        key = _webhook_receipt_key(operation["job_id"], operation["operation_id"], operation["tenant_id"])
        bucket_field, key_field = "webhook_bucket", "webhook_receipt_key"
    else:
        receipt.update(delivery_id=operation["operation_id"], delivery_status="success",
                       approval_status=operation["envelope"]["approval_status"], redacted_fields=operation["envelope"]["redacted_fields"],
                       webhook_status="success" if "webhook" in operation["providers"] else "skipped")
        key = _receipt_key(operation["job_id"], operation["operation_id"], operation["tenant_id"])
        bucket_field, key_field = "delivery_bucket", "delivery_receipt_key"
    for bucket in dict.fromkeys((settings.delivery_bucket, settings.validation_bucket)):
        receipt[bucket_field], receipt[key_field] = bucket, key
        try:
            upload_json(settings, bucket, key, receipt)
            return receipt
        except Exception:
            if bucket == settings.validation_bucket:
                raise

async def _process_work(repository, work):
    operation = await asyncio.to_thread(repository.get, work["tenant_id"], work["operation_id"])
    try:
        if work["provider"] == "__receipt__":
            result = await asyncio.wait_for(asyncio.to_thread(_project_receipt, operation), settings.delivery_request_timeout_seconds)
        elif operation["kind"] == "event":
            result = await _send_webhook_event(operation["target_url"], operation["operation_id"], operation["envelope"])
        else:
            context = DeliveryContext(settings, DeliveryRequest(**operation["request"]), operation["operation_id"], operation["envelope"], operation["target_url"])
            result = await asyncio.wait_for(build_provider(work["provider"]).deliver(context), settings.delivery_request_timeout_seconds)
        if work["provider"] != "__receipt__":
            if result.get("status") != "success":
                raise ValueError("Provider did not acknowledge delivery")
            result = {**result, "attempts": work["attempt_count"], "acknowledged_at": datetime.now(UTC).isoformat()}
        await asyncio.to_thread(repository.acknowledge, work, result)
    except Exception as exc:
        permanent = isinstance(exc, (ValueError, HTTPException))
        if isinstance(exc, httpx.HTTPStatusError):
            permanent = 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 425, 429)
        error = type(exc).__name__ + (f": HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else "")
        delay = min(300, max(1, settings.delivery_retry_backoff_seconds) * (2 ** min(work["budget_used"], 8)))
        await asyncio.to_thread(repository.fail, work, error, settings.delivery_durable_max_attempts, delay, permanent)
        logger.warning("delivery deferred operation=%s provider=%s error=%s", work["operation_id"], work["provider"], error)

async def _process_due(repository, tenant=None, operation=None):
    async with _DELIVERY_SEMAPHORE:
        lease = max(settings.delivery_lease_seconds, settings.delivery_request_timeout_seconds + 30, settings.delivery_webhook_timeout_seconds + 30)
        work = await asyncio.to_thread(repository.claim, lease, tenant, operation)
        if not work:
            return False
        await _process_work(repository, work)
        return True

async def _reconcile():
    while True:
        try:
            repository = get_repository()
            for _ in range(50):
                if not await _process_due(repository):
                    break
            backlog = await asyncio.to_thread(repository.backlog)
            PENDING.set(backlog["pending"])
            DEAD_LETTERS.set(backlog["dead_letter"])
            OLDEST.set(backlog["oldest"])
        except Exception:
            logger.exception("delivery reconciliation deferred")
        await asyncio.sleep(settings.delivery_reconcile_interval_seconds)

async def _submit(request, kind, operation_id, providers, envelope, target_url):
    require_artifact_scope(request.tenant_id, request.job_id, (_receipt_key if kind == "delivery" else _webhook_receipt_key)(request.job_id, operation_id, request.tenant_id))
    repository = get_repository()
    existing = await asyncio.to_thread(repository.get, request.tenant_id, operation_id)
    if not existing:
        try:
            legacy = await asyncio.to_thread(_legacy_receipt, request.tenant_id, request.job_id, operation_id, kind)
            if not legacy and kind == 'delivery' and not request.idempotency_key:
                old_id = 'delivery-' + hashlib.sha256(_stable_json({'job_id': request.job_id, 'payload': request.payload,
                    'approval_status': request.approval_status, 'destinations': providers,
                    'webhook_url': request.webhook_url or ''}).encode()).hexdigest()[:24]
                legacy = await asyncio.to_thread(_legacy_receipt, request.tenant_id, request.job_id, old_id, kind)
        except Exception as exc:
            raise HTTPException(503, "Cannot establish legacy delivery state") from exc
        if legacy:
            raise HTTPException(409, "Legacy receipt requires explicit ledger migration; delivery was not resent")
    fingerprint = hashlib.sha256(_stable_json({"request": request.model_dump(mode="json"), "providers": providers, "target_url": target_url}).encode()).hexdigest()
    stored = request.model_dump(mode="json")
    if kind == "delivery":
        stored["payload"], stored["metadata"] = envelope["payload"], envelope["metadata"]
    else:
        stored["data"] = envelope["data"]
    try:
        _, replay = await asyncio.to_thread(repository.register, request.tenant_id, operation_id, request.job_id, kind,
                                            fingerprint, stored, envelope, providers, target_url)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    for _ in range(len(providers) + 1):
        if not await _process_due(repository, request.tenant_id, operation_id):
            break
    operation = await asyncio.to_thread(repository.get, request.tenant_id, operation_id, request.job_id)
    return JSONResponse(status_code=200 if operation["state"] == "success" else 202,
                        content=jsonable_encoder(operation_response(operation, replay)))

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/ready")
async def ready():
    await asyncio.to_thread(get_repository().backlog)
    return {"status": "ready"}

@app.post("/deliver")
async def deliver(request: DeliveryRequest):
    _validate_approval(request)
    for key in (request.extraction_key, request.validation_key):
        if key:
            require_artifact_scope(request.tenant_id, request.job_id, key)
    providers = _provider_names(request)
    try:
        for provider in providers:
            build_provider(provider)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    target = _webhook_url(request) if "webhook" in providers else None
    if "webhook" in providers and target is None:
        raise HTTPException(422, "Webhook destination is missing")
    operation_id = _delivery_id(request, providers)
    payload, redacted = _redact_payload(request.payload)
    metadata, metadata_redacted = _redact_payload(request.metadata)
    envelope = {"schema_version": settings.delivery_payload_format_version, "delivery_id": operation_id,
                "job_id": request.job_id, "tenant_id": request.tenant_id, "approval_status": request.approval_status,
                "payload": payload, "metadata": metadata, "redacted_fields": sorted(set(redacted + metadata_redacted)),
                "source": {name: getattr(request, name) or "" for name in ("extraction_bucket", "extraction_key", "validation_bucket", "validation_key")},
                "prepared_at": datetime.now(UTC).isoformat()}
    return await _submit(request, "delivery", operation_id, providers, envelope, target)

@app.post("/webhooks/events")
async def deliver_webhook_event(request: WebhookEventRequest):
    operation_id = _webhook_event_id(request)
    target = _webhook_url(request)
    envelope = _webhook_envelope(request, operation_id)
    envelope["data"], _ = _redact_payload(envelope["data"])
    return await _submit(request, "event", operation_id, ["webhook"] if target else [], envelope, target)

@app.get("/deliveries/{job_id}/{delivery_id}")
async def get_delivery(job_id: str, delivery_id: str, tenant_id: str = "default"):
    operation = await asyncio.to_thread(get_repository().get, tenant_id, delivery_id, job_id)
    if not operation or operation["kind"] != "delivery":
        raise HTTPException(404, "Delivery not found")
    return operation_response(operation)

class RedriveRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)

@app.get("/delivery-queue")
async def delivery_queue(limit: int = 50, offset: int = 0, operator: Operator = Depends(resolve_operator)):
    return {"work": await asyncio.to_thread(get_repository().queue, operator.tenant_id, max(1, min(limit, 200)), max(0, offset))}

@app.post("/delivery-queue/{operation_id}/redrive", status_code=202)
async def redrive(operation_id: str, request: RedriveRequest, operator: Operator = Depends(resolve_operator)):
    try:
        found = await asyncio.to_thread(get_repository().redrive, operator.tenant_id, operation_id, operator.subject, request.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not found:
        raise HTTPException(404, "Delivery not found")
    return {"operation_id": operation_id, "status": "pending"}
