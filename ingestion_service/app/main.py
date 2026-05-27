from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import psycopg2
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from ingestion_service.app.auth import Principal, resolve_principal
from ingestion_service.app.repository import IngestionRepository, JobCreateInput, normalize_timestamp
from ingestion_service.app.schemas import AuditEventResponse, DocumentStatusResponse, DocumentSubmissionResponse, ListJobsResponse
from shared.idp_common.config import Settings, get_settings
from shared.idp_common.metrics import instrument_app
from shared.idp_common.storage import ensure_bucket, upload_bytes
from workflow_orchestrator.app.workflows import DocumentPipelineWorkflow

logger = logging.getLogger("ingestion_service")

INGESTION_SUBMISSIONS = Counter(
    "ingestion_submissions_total",
    "Number of document submissions by outcome",
    ["outcome", "source"],
)
INGESTION_UPLOAD_BYTES = Counter(
    "ingestion_upload_bytes_total",
    "Total bytes uploaded to ingestion",
)
INGESTION_IDEMPOTENCY_REPLAYS = Counter(
    "ingestion_idempotency_replays_total",
    "Count of replayed idempotent requests",
)
INGESTION_DEDUP_HITS = Counter(
    "ingestion_dedup_hits_total",
    "Count of deduplicated submissions",
)
INGESTION_QUEUE_PUBLISH = Counter(
    "ingestion_queue_publish_total",
    "Temporal queue publish attempts by outcome",
    ["outcome"],
)
INGESTION_UPLOAD_DURATION = Histogram(
    "ingestion_upload_duration_seconds",
    "Time spent uploading raw artifacts",
)
INGESTION_AUDIT_FAILURES = Counter(
    "ingestion_audit_failures_total",
    "Audit persistence failures",
)
INGESTION_FAILURES = Counter(
    "ingestion_failures_total",
    "Unhandled ingestion failures by type",
    ["error_type"],
)

ALLOWED_SOURCES = {"api", "email", "batch", "connector"}
FILENAME_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.repository = IngestionRepository(settings)
    app.state.temporal_client = None
    app.state.temporal_lock = asyncio.Lock()

    ensure_bucket(settings, settings.raw_bucket)
    logger.info("ingestion_service started")

    try:
        yield
    finally:
        repository: IngestionRepository = app.state.repository
        repository.close()
        logger.info("ingestion_service stopped")


app = FastAPI(title="ingestion_service", version="1.0.0", lifespan=lifespan)
instrument_app(app, "ingestion_service")


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


def get_repository() -> IngestionRepository:
    return app.state.repository


async def get_temporal_client(settings: Settings) -> Client:
    if app.state.temporal_client is not None:
        return app.state.temporal_client

    async with app.state.temporal_lock:
        if app.state.temporal_client is None:
            app.state.temporal_client = await Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
            )

    return app.state.temporal_client


def _parse_csv_set(raw: str) -> set[str]:
    return {value.strip().lower() for value in raw.split(",") if value.strip()}


def _sanitize_filename(name: str | None) -> str:
    if not name:
        return "document.bin"
    normalized = name.strip().split("/")[-1].split("\\")[-1]
    cleaned = FILENAME_SANITIZE_PATTERN.sub("_", normalized)
    cleaned = cleaned.strip("._")
    return cleaned or "document.bin"


def _detect_mime_signature(payload: bytes) -> str | None:
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_content_type(raw_content_type: str | None) -> str | None:
    if not raw_content_type:
        return None
    return raw_content_type.split(";", 1)[0].strip().lower()


def _validate_upload(
    settings: Settings,
    *,
    payload: bytes,
    content_type: str | None,
    filename: str,
) -> str:
    max_size = settings.ingestion_max_upload_size_mb * 1024 * 1024
    if len(payload) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max allowed size of {settings.ingestion_max_upload_size_mb}MB",
        )

    if len(payload) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    allowed_mime_types = _parse_csv_set(settings.ingestion_allowed_mime_types)
    allowed_extensions = _parse_csv_set(settings.ingestion_allowed_extensions)

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension and extension not in allowed_extensions:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: {extension}")

    normalized_content_type = _normalize_content_type(content_type)
    detected_mime = _detect_mime_signature(payload)

    if normalized_content_type and normalized_content_type not in allowed_mime_types and normalized_content_type != "application/octet-stream":
        raise HTTPException(status_code=415, detail=f"Unsupported content type: {normalized_content_type}")

    if detected_mime and detected_mime not in allowed_mime_types:
        raise HTTPException(status_code=415, detail=f"Unsupported file signature: {detected_mime}")

    guessed_mime = mimetypes.guess_type(filename)[0]
    guessed_mime = guessed_mime.lower() if guessed_mime else None

    effective_mime = detected_mime or normalized_content_type or guessed_mime
    if not effective_mime:
        raise HTTPException(status_code=415, detail="Unable to determine file type")

    if effective_mime not in allowed_mime_types:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {effective_mime}")

    return effective_mime


def _build_status_response(job: dict[str, Any], workflow_status: str | None = None) -> DocumentStatusResponse:
    effective_status = job["status"]
    if workflow_status and job["status"] not in {"FAILED"}:
        effective_status = workflow_status

    return DocumentStatusResponse(
        job_id=job["job_id"],
        tenant_id=job["tenant_id"],
        status=effective_status,
        source=job["source"],
        filename=job["original_filename"],
        content_type=job["content_type"],
        size_bytes=int(job["size_bytes"]),
        artifact_uri=job.get("artifact_uri"),
        workflow_id=job.get("workflow_id"),
        workflow_run_id=job.get("workflow_run_id"),
        workflow_status=workflow_status,
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
        created_at=normalize_timestamp(job.get("created_at")),
        updated_at=normalize_timestamp(job.get("updated_at")),
        metadata=job.get("metadata") or {},
    )


async def _audit(
    repository: IngestionRepository,
    *,
    job_id: str | None,
    tenant_id: str,
    actor_id: str,
    event_type: str,
    event_payload: dict[str, Any],
) -> None:
    try:
        await asyncio.to_thread(
            repository.add_audit_event,
            job_id=job_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            event_type=event_type,
            event_payload=event_payload,
        )
    except Exception:  # noqa: BLE001
        INGESTION_AUDIT_FAILURES.inc()
        logger.exception(
            "Failed to persist audit event job_id=%s tenant_id=%s event_type=%s",
            job_id,
            tenant_id,
            event_type,
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on path=%s", request.url.path, exc_info=exc)
    INGESTION_FAILURES.labels(type(exc).__name__).inc()
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.post("/documents", response_model=DocumentSubmissionResponse)
async def submit_document(
    file: UploadFile = File(...),
    source: str = Form(default="api"),
    external_reference: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
    repository: IngestionRepository = Depends(get_repository),
) -> DocumentSubmissionResponse:
    source = source.strip().lower()
    idempotency_key = idempotency_key.strip() if idempotency_key else None

    if source not in ALLOWED_SOURCES:
        raise HTTPException(status_code=422, detail=f"Invalid source. Expected one of: {sorted(ALLOWED_SOURCES)}")

    payload = await file.read()
    sanitized_filename = _sanitize_filename(file.filename)
    effective_mime = _validate_upload(settings, payload=payload, content_type=file.content_type, filename=sanitized_filename)

    sha256 = hashlib.sha256(payload).hexdigest()
    INGESTION_UPLOAD_BYTES.inc(len(payload))

    if idempotency_key:
        existing = await asyncio.to_thread(
            repository.get_job_by_idempotency,
            principal.tenant_id,
            idempotency_key,
        )
        if existing:
            INGESTION_IDEMPOTENCY_REPLAYS.inc()
            INGESTION_SUBMISSIONS.labels("idempotent_replay", source).inc()
            await _audit(
                repository,
                job_id=existing["job_id"],
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                event_type="idempotency_replay",
                event_payload={"idempotency_key": idempotency_key},
            )
            return DocumentSubmissionResponse(
                job_id=existing["job_id"],
                status=existing["status"],
                workflow_id=existing.get("workflow_id"),
                workflow_run_id=existing.get("workflow_run_id"),
                artifact_uri=existing.get("artifact_uri") or "",
                idempotency_replay=True,
                deduplicated=False,
                status_url=f"/documents/{existing['job_id']}",
            )

    dedupe_match = await asyncio.to_thread(
        repository.get_recent_job_by_hash,
        principal.tenant_id,
        sha256,
        settings.ingestion_dedupe_window_hours,
    )
    if dedupe_match and dedupe_match["status"] != "FAILED":
        INGESTION_DEDUP_HITS.inc()
        INGESTION_SUBMISSIONS.labels("deduplicated", source).inc()
        await _audit(
            repository,
            job_id=dedupe_match["job_id"],
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            event_type="duplicate_detected",
            event_payload={"sha256": sha256},
        )
        return DocumentSubmissionResponse(
            job_id=dedupe_match["job_id"],
            status=dedupe_match["status"],
            workflow_id=dedupe_match.get("workflow_id"),
            workflow_run_id=dedupe_match.get("workflow_run_id"),
            artifact_uri=dedupe_match.get("artifact_uri") or "",
            idempotency_replay=False,
            deduplicated=True,
            status_url=f"/documents/{dedupe_match['job_id']}",
        )

    job_id = str(uuid.uuid4())
    metadata = {
        "external_reference": external_reference,
        "ingestion_auth_scheme": principal.auth_scheme,
        "submitted_by": principal.subject,
    }

    try:
        await asyncio.to_thread(
            repository.create_job,
            JobCreateInput(
                job_id=job_id,
                tenant_id=principal.tenant_id,
                source=source,
                idempotency_key=idempotency_key,
                sha256=sha256,
                original_filename=sanitized_filename,
                content_type=effective_mime,
                size_bytes=len(payload),
                metadata=metadata,
            ),
        )
    except psycopg2.IntegrityError as exc:
        if idempotency_key and getattr(exc, "pgcode", "") == "23505":
            existing = await asyncio.to_thread(
                repository.get_job_by_idempotency,
                principal.tenant_id,
                idempotency_key,
            )
            if existing:
                return DocumentSubmissionResponse(
                    job_id=existing["job_id"],
                    status=existing["status"],
                    workflow_id=existing.get("workflow_id"),
                    workflow_run_id=existing.get("workflow_run_id"),
                    artifact_uri=existing.get("artifact_uri") or "",
                    idempotency_replay=True,
                    deduplicated=False,
                    status_url=f"/documents/{existing['job_id']}",
                )
        raise HTTPException(status_code=500, detail="Unable to register ingestion metadata") from exc

    await _audit(
        repository,
        job_id=job_id,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject,
        event_type="job_registered",
        event_payload={
            "source": source,
            "filename": sanitized_filename,
            "content_type": effective_mime,
            "size_bytes": len(payload),
        },
    )

    key = f"jobs/{job_id}/raw/{sanitized_filename}"
    try:
        with INGESTION_UPLOAD_DURATION.time():
            artifact_uri = upload_bytes(
                settings=settings,
                bucket=settings.raw_bucket,
                key=key,
                payload=payload,
                content_type=effective_mime,
            )
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(repository.mark_failed, job_id, "ARTIFACT_UPLOAD_FAILED", str(exc))
        await _audit(
            repository,
            job_id=job_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            event_type="artifact_upload_failed",
            event_payload={"error": str(exc)},
        )
        INGESTION_SUBMISSIONS.labels("failed", source).inc()
        raise HTTPException(status_code=503, detail="Raw artifact persistence failed") from exc

    await asyncio.to_thread(repository.update_artifact, job_id, settings.raw_bucket, key, artifact_uri)
    await _audit(
        repository,
        job_id=job_id,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject,
        event_type="artifact_persisted",
        event_payload={"artifact_uri": artifact_uri},
    )

    workflow_id = f"idp-{principal.tenant_id}-{job_id}"
    workflow_run_id: str | None = None
    start_payload = {
        "job_id": job_id,
        "tenant_id": principal.tenant_id,
        "source": source,
        "raw_bucket": settings.raw_bucket,
        "raw_key": key,
    }

    try:
        temporal_client = await get_temporal_client(settings)
        handle = await temporal_client.start_workflow(
            DocumentPipelineWorkflow.run,
            start_payload,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
            execution_timeout=timedelta(minutes=settings.ingestion_workflow_timeout_minutes),
        )
        workflow_run_id = getattr(handle, "first_execution_run_id", None)
        await asyncio.to_thread(repository.mark_queued, job_id, workflow_id, workflow_run_id)
        INGESTION_QUEUE_PUBLISH.labels("success").inc()
    except WorkflowAlreadyStartedError:
        await asyncio.to_thread(repository.mark_queued, job_id, workflow_id, None)
        INGESTION_QUEUE_PUBLISH.labels("already_started").inc()
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(repository.mark_failed, job_id, "QUEUE_PUBLISH_FAILED", str(exc))
        await _audit(
            repository,
            job_id=job_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            event_type="queue_publish_failed",
            event_payload={"error": str(exc)},
        )
        INGESTION_QUEUE_PUBLISH.labels("failed").inc()
        INGESTION_SUBMISSIONS.labels("failed", source).inc()
        raise HTTPException(status_code=503, detail="Unable to queue workflow") from exc

    await _audit(
        repository,
        job_id=job_id,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject,
        event_type="workflow_queued",
        event_payload={"workflow_id": workflow_id, "task_queue": settings.temporal_task_queue},
    )

    INGESTION_SUBMISSIONS.labels("accepted", source).inc()
    logger.info("ingestion accepted job_id=%s tenant_id=%s source=%s", job_id, principal.tenant_id, source)

    return DocumentSubmissionResponse(
        job_id=job_id,
        status="QUEUED",
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        artifact_uri=artifact_uri,
        idempotency_replay=False,
        deduplicated=False,
        status_url=f"/documents/{job_id}",
    )


@app.get("/documents/{job_id}", response_model=DocumentStatusResponse)
async def document_status(
    job_id: str,
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
    repository: IngestionRepository = Depends(get_repository),
) -> DocumentStatusResponse:
    job = await asyncio.to_thread(repository.get_job_by_id, principal.tenant_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    workflow_status: str | None = None
    workflow_id = job.get("workflow_id")
    if workflow_id:
        try:
            client = await get_temporal_client(settings)
            workflow = client.get_workflow_handle(workflow_id)
            description = await workflow.describe()
            workflow_status = description.status.name
        except Exception:  # noqa: BLE001
            workflow_status = None

    return _build_status_response(job, workflow_status)


@app.get("/documents", response_model=ListJobsResponse)
async def list_documents(
    limit: int = 50,
    principal: Principal = Depends(resolve_principal),
    repository: IngestionRepository = Depends(get_repository),
) -> ListJobsResponse:
    bounded_limit = max(1, min(limit, 200))
    jobs = await asyncio.to_thread(repository.list_jobs, principal.tenant_id, bounded_limit)
    return ListJobsResponse(jobs=[_build_status_response(job) for job in jobs])


@app.get("/documents/{job_id}/audit", response_model=list[AuditEventResponse])
async def document_audit_events(
    job_id: str,
    limit: int = 100,
    principal: Principal = Depends(resolve_principal),
    repository: IngestionRepository = Depends(get_repository),
) -> list[AuditEventResponse]:
    bounded_limit = max(1, min(limit, 500))
    events = await asyncio.to_thread(repository.get_audit_events, principal.tenant_id, job_id, bounded_limit)
    return [
        AuditEventResponse(
            id=int(event["id"]),
            job_id=event.get("job_id"),
            tenant_id=event["tenant_id"],
            actor_id=event["actor_id"],
            event_type=event["event_type"],
            event_payload=event.get("event_payload") or {},
            created_at=normalize_timestamp(event["created_at"]) or "",
        )
        for event in events
    ]


@app.get("/documents/{job_id}/result")
async def document_result(
    job_id: str,
    principal: Principal = Depends(resolve_principal),
    settings: Settings = Depends(get_settings),
    repository: IngestionRepository = Depends(get_repository),
) -> dict[str, Any]:
    job = await asyncio.to_thread(repository.get_job_by_id, principal.tenant_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    workflow_id = job.get("workflow_id")
    if not workflow_id:
        raise HTTPException(status_code=409, detail="Workflow has not been queued yet")

    client = await get_temporal_client(settings)
    handle = client.get_workflow_handle(workflow_id)
    try:
        result = await handle.result()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail="Workflow result not available") from exc

    return {"job_id": job_id, "result": result}
