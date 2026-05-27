from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor

from shared.idp_common.config import Settings


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
        self._pool = pool.SimpleConnectionPool(
            minconn=settings.ingestion_db_pool_min,
            maxconn=max(settings.ingestion_db_pool_max, settings.ingestion_db_pool_min),
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
        )
        self._ensure_schema()

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

    def create_job(self, job: JobCreateInput) -> dict[str, Any]:
        with self._cursor() as (_, cur):
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
                SELECT *
                FROM ingestion_jobs
                WHERE tenant_id = %s AND idempotency_key = %s
                ORDER BY created_at DESC
                LIMIT 1
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
                WHERE job_id = %s
                RETURNING *
                """,
                (bucket, key, uri, job_id),
            )
            row = cur.fetchone()

        return dict(row) if row else {}

    def mark_queued(self, job_id: str, workflow_id: str, workflow_run_id: str | None) -> dict[str, Any]:
        with self._cursor() as (_, cur):
            cur.execute(
                """
                UPDATE ingestion_jobs
                SET
                    status = 'QUEUED',
                    workflow_id = %s,
                    workflow_run_id = %s,
                    updated_at = NOW()
                WHERE job_id = %s
                RETURNING *
                """,
                (workflow_id, workflow_run_id, job_id),
            )
            row = cur.fetchone()

        return dict(row) if row else {}

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
                WHERE job_id = %s
                RETURNING *
                """,
                (error_code, error_message[:500], job_id),
            )
            row = cur.fetchone()

        return dict(row) if row else {}

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


def normalize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
