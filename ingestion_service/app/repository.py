from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import psycopg2
import uuid
from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor

from shared.idp_common.config import Settings
from shared.idp_common.tenant_scope import require_artifact_scope


@dataclass(frozen=True)
class JobCreateInput:
    job_id: str
    tenant_id: str
    source: str
    idempotency_key: str | None
    sha256: str
    original_filename: str
    content_type: str
    size_bytes: int
    metadata: dict[str, Any]


class IngestionRepository:
    def __init__(self, settings: Settings) -> None:
        self._pool = pool.ThreadedConnectionPool(
            minconn=settings.ingestion_db_pool_min,
            maxconn=max(settings.ingestion_db_pool_max, settings.ingestion_db_pool_min),
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
        )
        try:
            self._ensure_schema()
        except Exception:
            self._pool.closeall()
            raise

    @contextmanager
    def _cursor(self) -> Iterator[tuple[Any, Any]]:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield conn, cur
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _ensure_schema(self) -> None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    sha256 TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,
                    artifact_bucket TEXT,
                    artifact_key TEXT,
                    artifact_uri TEXT,
                    workflow_id TEXT,
                    workflow_run_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ingestion_jobs_tenant_idempotency_idx
                ON ingestion_jobs (tenant_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ingestion_jobs_tenant_hash_created_idx
                ON ingestion_jobs (tenant_id, sha256, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_audit_events (
                    id BIGSERIAL PRIMARY KEY,
                    job_id TEXT,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ingestion_audit_events_job_created_idx
                ON ingestion_audit_events (job_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_dispatch_outbox (
                    job_id TEXT PRIMARY KEY REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL,
                    raw_bucket TEXT NOT NULL,
                    raw_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    lease_until TIMESTAMPTZ,
                    last_error TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ingestion_dispatch_due_idx
                ON ingestion_dispatch_outbox(next_attempt_at)
                WHERE status = 'pending'
                """
            )

            cur.execute("ALTER TABLE ingestion_dispatch_outbox ADD COLUMN IF NOT EXISTS lease_token TEXT")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ingestion_idempotency_requests (
                    tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE,
                    PRIMARY KEY (tenant_id, idempotency_key)
                )
            """)
            cur.execute("""
                INSERT INTO ingestion_idempotency_requests SELECT tenant_id, idempotency_key, sha256, job_id
                FROM ingestion_jobs WHERE idempotency_key IS NOT NULL ON CONFLICT DO NOTHING
            """)
            cur.execute("""CREATE TABLE IF NOT EXISTS ingestion_reprocess_requests (
                tenant_id TEXT NOT NULL, parent_job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE,
                child_job_id TEXT NOT NULL UNIQUE REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE,
                request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, actor_id TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'requested', outcome TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(tenant_id, parent_job_id))""")
            cur.execute("""ALTER TABLE ingestion_reprocess_requests ADD COLUMN IF NOT EXISTS
                           last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()""")

    def bind_idempotency(self, tenant_id: str, key: str, sha256: str, job_id: str) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            cur.execute("""INSERT INTO ingestion_idempotency_requests
                           SELECT tenant_id, %s, sha256, job_id FROM ingestion_jobs
                           WHERE job_id = %s AND tenant_id = %s AND sha256 = %s
                           ON CONFLICT DO NOTHING""", (key, job_id, tenant_id, sha256))
            cur.execute("""SELECT j.*, r.sha256 AS request_sha256 FROM ingestion_jobs j
                           JOIN ingestion_idempotency_requests r ON r.job_id = j.job_id
                           WHERE r.tenant_id = %s AND r.idempotency_key = %s AND j.tenant_id = r.tenant_id""", (tenant_id, key))
            row = cur.fetchone()
            if not row or row["request_sha256"] != sha256:
                raise ValueError("Idempotency key was already used for different content")
            return dict(row)

    def create_job(self, job: JobCreateInput, dedupe_window_hours: int | None = None) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            if dedupe_window_hours is not None:
                # Serialize equal content within a tenant, not unrelated uploads.
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{job.tenant_id}:{job.sha256}",))
                cur.execute("""SELECT * FROM ingestion_jobs WHERE tenant_id = %s AND sha256 = %s
                               AND status != 'FAILED' AND created_at >= NOW() - make_interval(hours => %s)
                               ORDER BY created_at DESC LIMIT 1""", (job.tenant_id, job.sha256, dedupe_window_hours))
                existing = cur.fetchone()
                if existing:
                    if job.idempotency_key:
                        cur.execute("INSERT INTO ingestion_idempotency_requests VALUES (%s, %s, %s, %s)",
                                    (job.tenant_id, job.idempotency_key, job.sha256, existing["job_id"]))
                    return dict(existing)
            cur.execute(
                """
                INSERT INTO ingestion_jobs (
                    job_id,
                    tenant_id,
                    source,
                    status,
                    idempotency_key,
                    sha256,
                    original_filename,
                    content_type,
                    size_bytes,
                    metadata
                )
                VALUES (%s, %s, %s, 'RECEIVED', %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    job.job_id,
                    job.tenant_id,
                    job.source,
                    job.idempotency_key,
                    job.sha256,
                    job.original_filename,
                    job.content_type,
                    job.size_bytes,
                    Json(job.metadata),
                ),
            )
            row = cur.fetchone()

            if job.idempotency_key:
                cur.execute("INSERT INTO ingestion_idempotency_requests VALUES (%s, %s, %s, %s)",
                            (job.tenant_id, job.idempotency_key, job.sha256, job.job_id))
            self._audit_in_transaction(cur, job.job_id, job.tenant_id,
                                       str(job.metadata.get("submitted_by") or "ingestion-service"), "job_registered",
                                       {"source": job.source, "filename": job.original_filename,
                                        "content_type": job.content_type, "size_bytes": job.size_bytes})

        return dict(row) if row else {}

    def get_job_by_id(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                SELECT *
                FROM ingestion_jobs
                WHERE tenant_id = %s AND job_id = %s
                LIMIT 1
                """,
                (tenant_id, job_id),
            )
            row = cur.fetchone()

        return dict(row) if row else None

    def get_job_by_idempotency(self, tenant_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                SELECT j.* FROM ingestion_jobs j JOIN ingestion_idempotency_requests r ON r.job_id = j.job_id
                WHERE r.tenant_id = %s AND r.idempotency_key = %s AND j.tenant_id = r.tenant_id
                """,
                (tenant_id, idempotency_key),
            )
            row = cur.fetchone()

        return dict(row) if row else None

    def get_recent_job_by_hash(self, tenant_id: str, sha256: str, window_hours: int) -> dict[str, Any] | None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                SELECT *
                FROM ingestion_jobs
                WHERE tenant_id = %s
                  AND sha256 = %s
                  AND created_at >= NOW() - make_interval(hours => %s)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (tenant_id, sha256, window_hours),
            )
            row = cur.fetchone()

        return dict(row) if row else None

    def update_artifact(self, job_id: str, bucket: str, key: str, uri: str) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                UPDATE ingestion_jobs
                SET
                    artifact_bucket = %s,
                    artifact_key = %s,
                    artifact_uri = %s,
                    status = 'STORED',
                    updated_at = NOW()
                WHERE job_id = %s AND status = 'RECEIVED'
                RETURNING *
                """,
                (bucket, key, uri, job_id),
            )
            row = cur.fetchone()
            if row:
                require_artifact_scope(row["tenant_id"], job_id, key)
                cur.execute(
                    """
                    INSERT INTO ingestion_dispatch_outbox(job_id, tenant_id, raw_bucket, raw_key)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                    """,
                    (job_id, row["tenant_id"], bucket, key),
                )
                self._audit_in_transaction(cur, job_id, row["tenant_id"], "ingestion-service",
                                           "artifact_persisted", {"artifact_uri": uri})

        return dict(row) if row else {}

    def mark_queued(self, job_id: str, workflow_id: str, workflow_run_id: str | None, lease_token: str) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            cur.execute("SELECT job_id FROM ingestion_dispatch_outbox WHERE job_id = %s AND lease_token = %s AND status = 'pending' FOR UPDATE", (job_id, lease_token))
            if not cur.fetchone():
                raise RuntimeError("Dispatch lease was superseded")
            cur.execute(
                """
                UPDATE ingestion_jobs
                SET
                    status = 'QUEUED',
                    workflow_id = %s,
                    workflow_run_id = COALESCE(%s, workflow_run_id),
                    updated_at = NOW()
                WHERE job_id = %s AND status IN ('STORED', 'QUEUED')
                RETURNING *
                """,
                (workflow_id, workflow_run_id, job_id),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Job cannot be marked queued without a stored artifact")
            cur.execute(
                """
                UPDATE ingestion_dispatch_outbox
                SET status = 'dispatched', lease_until = NULL, updated_at = NOW()
                WHERE job_id = %s
                """,
                (job_id,),
            )
            self._audit_in_transaction(cur, job_id, row["tenant_id"], "ingestion-dispatcher",
                                       "workflow_queued", {"workflow_id": workflow_id, "workflow_run_id": workflow_run_id})
            cur.execute("""UPDATE ingestion_reprocess_requests SET status = 'dispatched', updated_at = NOW()
                           WHERE child_job_id = %s AND tenant_id = %s AND status = 'requested'""", (job_id, row["tenant_id"]))

        return dict(row) if row else {}

    def claim_dispatch(self, job_id: str | None = None) -> dict[str, Any] | None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                WITH candidate AS (
                    SELECT job_id FROM ingestion_dispatch_outbox
                    WHERE status = 'pending'
                      AND next_attempt_at <= NOW()
                      AND (lease_until IS NULL OR lease_until < NOW())
                      AND (%s IS NULL OR job_id = %s)
                    ORDER BY next_attempt_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE ingestion_dispatch_outbox AS d
                SET lease_until = NOW() + INTERVAL '2 minutes', lease_token = %s,
                    attempts = attempts + 1, updated_at = NOW()
                FROM candidate
                WHERE d.job_id = candidate.job_id
                RETURNING d.*
                """,
                (job_id, job_id, uuid.uuid4().hex),
            )
            row = cur.fetchone()
            if row:
                row = dict(row)
                cur.execute("SELECT metadata FROM ingestion_jobs WHERE job_id=%s AND tenant_id=%s", (row['job_id'], row['tenant_id']))
                row['metadata'] = cur.fetchone()['metadata']
        return dict(row) if row else None

    def list_incomplete_uploads(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                SELECT job_id, tenant_id, original_filename, sha256, size_bytes, content_type, metadata,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_seconds
                FROM ingestion_jobs
                WHERE status = 'RECEIVED' AND created_at < NOW() - INTERVAL '2 minutes'
                ORDER BY created_at
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def release_dispatch(self, job_id: str, error: str, lease_token: str) -> None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                UPDATE ingestion_dispatch_outbox
                SET lease_until = NULL, lease_token = NULL,
                    next_attempt_at = NOW() + make_interval(secs => LEAST(300, 5 * POWER(2, LEAST(attempts, 6)))::integer),
                    last_error = %s, updated_at = NOW()
                WHERE job_id = %s AND status = 'pending' AND lease_token = %s
                """,
                (error[:500], job_id, lease_token),
            )

    def mark_failed(self, job_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                UPDATE ingestion_jobs
                SET
                    status = 'FAILED',
                    error_code = %s,
                    error_message = %s,
                    updated_at = NOW()
                WHERE job_id = %s AND status = 'RECEIVED'
                RETURNING *
                """,
                (error_code, error_message[:500], job_id),
            )
            row = cur.fetchone()
            if row:
                self._audit_in_transaction(cur, job_id, row["tenant_id"], "ingestion-reconciler",
                                           "job_failed", {"error_code": error_code, "error_message": error_message[:500]})

        return dict(row) if row else {}

    @staticmethod
    def _audit_in_transaction(cur: Any, job_id: str, tenant_id: str, actor_id: str,
                              event_type: str, event_payload: dict[str, Any]) -> None:
        cur.execute("""INSERT INTO ingestion_audit_events(job_id, tenant_id, actor_id, event_type, event_payload)
                       VALUES (%s, %s, %s, %s, %s)""", (job_id, tenant_id, actor_id, event_type, Json(event_payload)))

    def add_audit_event(
        self,
        *,
        job_id: str | None,
        tenant_id: str,
        actor_id: str,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> None:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                INSERT INTO ingestion_audit_events (job_id, tenant_id, actor_id, event_type, event_payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (job_id, tenant_id, actor_id, event_type, Json(event_payload)),
            )

    def get_audit_events(self, tenant_id: str, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                SELECT id, job_id, tenant_id, actor_id, event_type, event_payload, created_at
                FROM ingestion_audit_events
                WHERE tenant_id = %s AND job_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, job_id, limit),
            )
            rows = cur.fetchall()

        return [dict(row) for row in rows]

    def list_jobs(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                SELECT *
                FROM ingestion_jobs
                WHERE tenant_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            rows = cur.fetchall()

        return [dict(row) for row in rows]

    def close(self) -> None:
        self._pool.closeall()

    def get_reprocess_request(self, tenant_id: str, parent_job_id: str) -> dict[str, Any] | None:
        with self._cursor() as (_, cur):
            cur.execute("SELECT * FROM ingestion_reprocess_requests WHERE tenant_id = %s AND parent_job_id = %s",
                        (tenant_id, parent_job_id))
            row = cur.fetchone()
        return dict(row) if row else None

    def create_reprocess_job(self, tenant_id: str, parent_job_id: str, child_job_id: str,
                            request_key: str, fingerprint: str, actor_id: str, reason: str) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            cur.execute("SELECT * FROM ingestion_jobs WHERE tenant_id = %s AND job_id = %s FOR UPDATE",
                        (tenant_id, parent_job_id))
            parent = cur.fetchone()
            if not parent:
                raise ValueError("Parent job not found")
            cur.execute("SELECT * FROM ingestion_reprocess_requests WHERE tenant_id = %s AND parent_job_id = %s",
                        (tenant_id, parent_job_id))
            existing = cur.fetchone()
            if existing:
                if existing["request_key"] != request_key or existing["fingerprint"] != fingerprint:
                    raise ValueError("An immutable reprocessing request already exists for this exception")
                return dict(existing)
            if not parent["artifact_bucket"] or not parent["artifact_key"]:
                raise ValueError("Source artifact expired before replacement registration")
            metadata = {"submitted_by": actor_id, "reprocess_parent_job_id": parent_job_id,
                        "reprocess_generation": int((parent["metadata"] or {}).get("reprocess_generation", 0)) + 1,
                        "reprocess_source": {"tenant_id": tenant_id, "job_id": parent_job_id,
                                             "bucket": parent["artifact_bucket"], "key": parent["artifact_key"]}}
            cur.execute("""INSERT INTO ingestion_jobs(job_id, tenant_id, source, status, sha256,
                           original_filename, content_type, size_bytes, metadata)
                           VALUES (%s, %s, 'api', 'RECEIVED', %s, %s, %s, %s, %s)""",
                        (child_job_id, tenant_id, parent["sha256"], parent["original_filename"],
                         parent["content_type"], parent["size_bytes"], Json(metadata)))
            cur.execute("""INSERT INTO ingestion_reprocess_requests(tenant_id, parent_job_id, child_job_id,
                           request_key, fingerprint, actor_id, reason) VALUES (%s, %s, %s, %s, %s, %s, %s)
                           RETURNING *""", (tenant_id, parent_job_id, child_job_id, request_key, fingerprint, actor_id, reason))
            row = dict(cur.fetchone())
            self._audit_in_transaction(cur, child_job_id, tenant_id, actor_id, "job_registered", {"parent_job_id": parent_job_id})
            self._audit_in_transaction(cur, parent_job_id, tenant_id, actor_id, "reprocess_requested",
                                       {"child_job_id": child_job_id, "reason": reason})
            return row

    def list_reprocess_inflight(self) -> list[dict[str, Any]]:
        with self._cursor() as (_, cur):
            cur.execute("""SELECT r.*, j.workflow_id, j.status AS job_status FROM ingestion_reprocess_requests r
                           JOIN ingestion_jobs j ON j.job_id = r.child_job_id AND j.tenant_id = r.tenant_id
                           WHERE r.status IN ('requested', 'dispatched') ORDER BY r.last_checked_at LIMIT 100""")
            return [dict(row) for row in cur.fetchall()]

    def record_reprocess_check(self, tenant_id: str, parent_job_id: str) -> None:
        with self._cursor() as (_, cur):
            cur.execute("UPDATE ingestion_reprocess_requests SET last_checked_at=NOW() WHERE tenant_id=%s AND parent_job_id=%s",
                        (tenant_id, parent_job_id))

    def set_reprocess_status(self, tenant_id: str, parent_job_id: str, status: str, outcome: str) -> None:
        with self._cursor() as (_, cur):
            cur.execute("""UPDATE ingestion_reprocess_requests SET status = %s, outcome = %s, updated_at = NOW()
                           WHERE tenant_id = %s AND parent_job_id = %s AND status IN ('requested', 'dispatched')
                           RETURNING child_job_id""", (status, outcome, tenant_id, parent_job_id))
            row = cur.fetchone()
            if row:
                self._audit_in_transaction(cur, parent_job_id, tenant_id, "ingestion-reconciler", "reprocess_" + status,
                                           {"child_job_id": row["child_job_id"], "outcome": outcome})


def normalize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
