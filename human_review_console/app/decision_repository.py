from __future__ import annotations

from typing import Any

from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor

from shared.idp_common.config import Settings


class ReviewDecisionRepository:
    def __init__(self, settings: Settings) -> None:
        self._pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=max(2, settings.review_max_inflight_requests, settings.ingestion_db_pool_max),
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
        )
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS review_decisions (
                        tenant_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        review_task_id TEXT NOT NULL,
                        decision JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (tenant_id, job_id, review_task_id)
                    )
                """)
            conn.commit()
        except Exception:
            conn.rollback()
            self._pool.putconn(conn)
            self._pool.closeall()
            raise
        self._pool.putconn(conn)

    def get(self, tenant_id: str, job_id: str, review_task_id: str) -> dict[str, Any] | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT decision FROM review_decisions WHERE tenant_id = %s AND job_id = %s AND review_task_id = %s",
                    (tenant_id, job_id, review_task_id),
                )
                row = cur.fetchone()
            return dict(row["decision"]) if row else None
        finally:
            conn.rollback()
            self._pool.putconn(conn)

    def create(self, decision: dict[str, Any]) -> dict[str, Any]:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO review_decisions (tenant_id, job_id, review_task_id, decision)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING decision
                    """,
                    (decision["tenant_id"], decision["job_id"], decision["review_task_id"], Json(decision)),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        "SELECT decision FROM review_decisions WHERE tenant_id = %s AND job_id = %s AND review_task_id = %s",
                        (decision["tenant_id"], decision["job_id"], decision["review_task_id"]),
                    )
                    row = cur.fetchone()
            conn.commit()
            return dict(row["decision"])
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.closeall()

    def list_exceptions(self, tenant_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT decision FROM review_decisions
                               WHERE tenant_id = %s AND review_task_id = 'ocr-exception'
                               ORDER BY created_at, job_id LIMIT %s OFFSET %s""", (tenant_id, limit, offset))
                return [dict(row["decision"]) for row in cur.fetchall()]
        finally:
            conn.rollback()
            self._pool.putconn(conn)
