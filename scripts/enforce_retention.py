#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import psycopg2

from shared.idp_common.config import get_settings
from shared.idp_common.storage import get_s3_client
from shared.idp_common.tenant_scope import artifact_prefix


@dataclass(frozen=True)
class RetentionPolicy:
    raw_days: int
    derived_days: int
    audit_days: int


def policy_from_env() -> RetentionPolicy:
    try:
        policy = RetentionPolicy(
            raw_days=int(os.environ["RAW_ARTIFACT_RETENTION_DAYS"]),
            derived_days=int(os.environ["DERIVED_ARTIFACT_RETENTION_DAYS"]),
            audit_days=int(os.environ["AUDIT_RETENTION_DAYS"]),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("retention policy variables must be configured as integers") from exc
    if min(policy.raw_days, policy.derived_days, policy.audit_days) < 1:
        raise ValueError("retention periods must be positive")
    if policy.derived_days < policy.raw_days:
        raise ValueError("derived retention must be at least raw retention")
    if policy.audit_days < policy.derived_days:
        raise ValueError("audit retention must be at least derived retention")
    return policy


def delete_prefix(s3: Any, bucket: str, prefix: str) -> int:
    deleted = 0
    continuation_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = s3.list_objects_v2(**kwargs)
        objects = [{"Key": item["Key"]} for item in response.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
            deleted += len(objects)
        if not response.get("IsTruncated"):
            return deleted
        continuation_token = response.get("NextContinuationToken")


def retention_holds(cursor: Any, *, apply: bool) -> tuple[set[str], set[str]]:
    tables = set()
    for name in ("ingestion_reprocess_requests", "delivery_operations", "delivery_work"):
        cursor.execute("SELECT to_regclass(%s)", ("public." + name,))
        if cursor.fetchone()[0] is not None:
            tables.add(name)
    if apply:
        # Maintenance locks serialize registration/redrive with the deletion snapshot.
        cursor.execute("LOCK TABLE ingestion_jobs IN ACCESS EXCLUSIVE MODE")
        for name in ("ingestion_reprocess_requests", "delivery_operations", "delivery_work"):
            if name in tables:
                cursor.execute(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE")
    held = set()
    if "ingestion_reprocess_requests" in tables:
        cursor.execute("SELECT parent_job_id, child_job_id FROM ingestion_reprocess_requests WHERE status IN ('requested', 'dispatched')")
        for parent, child in cursor.fetchall():
            held.update((str(parent), str(child)))
    if {"delivery_operations", "delivery_work"} <= tables:
        cursor.execute("""
            SELECT o.job_id FROM delivery_operations o
            WHERE EXISTS (SELECT 1 FROM delivery_work w WHERE w.tenant_id=o.tenant_id
                AND w.operation_id=o.operation_id AND w.state IN ('pending', 'leased'))
            AND NOT EXISTS (SELECT 1 FROM delivery_work w WHERE w.tenant_id=o.tenant_id
                AND w.operation_id=o.operation_id AND w.state='dead_letter')
        """)
        held.update(str(row[0]) for row in cursor.fetchall())
    return held, tables


def enforce_retention(*, apply: bool) -> dict[str, Any]:
    settings = get_settings()
    policy = policy_from_env()
    connection = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
    )
    try:
        with connection.cursor() as cursor:
            held_jobs, tables = retention_holds(cursor, apply=apply)
            cursor.execute(
                """
                SELECT job_id, artifact_bucket, artifact_key
                FROM ingestion_jobs
                WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                  AND artifact_bucket IS NOT NULL
                  AND artifact_key IS NOT NULL
                """,
                (policy.raw_days,),
            )
            raw_candidates = [row for row in cursor.fetchall() if str(row[0]) not in held_jobs]
            cursor.execute(
                """
                SELECT tenant_id, job_id
                FROM ingestion_jobs
                WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                """,
                (policy.derived_days,),
            )
            expired_jobs = [(str(row[0]), str(row[1])) for row in cursor.fetchall() if str(row[1]) not in held_jobs]
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM ingestion_audit_events
                WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                """,
                (policy.audit_days,),
            )
            expired_audit_count = int(cursor.fetchone()[0])

        deleted_objects = 0
        if apply:
            s3 = get_s3_client(settings)
            for _, bucket, key in raw_candidates:
                s3.delete_object(Bucket=bucket, Key=key)
                deleted_objects += 1

            derived_buckets = {
                settings.preprocessed_bucket,
                settings.ocr_bucket,
                settings.layout_bucket,
                settings.extraction_bucket,
                settings.validation_bucket,
                settings.review_bucket,
                settings.delivery_bucket,
                settings.evaluation_bucket,
            }
            for tenant_id, job_id in expired_jobs:
                for bucket in derived_buckets:
                    deleted_objects += delete_prefix(s3, bucket, artifact_prefix(tenant_id, job_id))

            with connection.cursor() as cursor:
                expired_ids = [job_id for _, job_id in expired_jobs]
                cursor.execute("SELECT to_regclass('public.ingestion_dispatch_outbox')")
                if cursor.fetchone()[0] is not None:
                    cursor.execute(
                        """
                        DELETE FROM ingestion_dispatch_outbox
                        WHERE job_id = ANY(%s::text[])
                        """,
                        (expired_ids,),
                    )
                cursor.execute("SELECT to_regclass('public.review_decisions')")
                if cursor.fetchone()[0] is not None:
                    cursor.execute(
                        """
                        DELETE FROM review_decisions
                        WHERE job_id = ANY(%s::text[])
                        """,
                        (expired_ids,),
                    )
                if "delivery_operations" in tables:
                    cursor.execute("DELETE FROM delivery_operations WHERE job_id = ANY(%s::text[])", (expired_ids,))
                cursor.execute("SELECT to_regclass('public.delivery_operator_audit')")
                if cursor.fetchone()[0] is not None:
                    cursor.execute("DELETE FROM delivery_operator_audit WHERE created_at < NOW() - (%s * INTERVAL '1 day')", (policy.audit_days,))
                cursor.execute(
                    """
                    UPDATE ingestion_jobs
                    SET artifact_bucket = NULL, artifact_key = NULL, artifact_uri = NULL, updated_at = NOW()
                    WHERE job_id = ANY(%s::text[])
                    """,
                    ([str(row[0]) for row in raw_candidates],),
                )
                cursor.execute(
                    """
                    DELETE FROM ingestion_jobs
                    WHERE job_id = ANY(%s::text[])
                    """,
                    (expired_ids,),
                )
                deleted_job_count = cursor.rowcount
                cursor.execute(
                    """
                    DELETE FROM ingestion_audit_events
                    WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                    """,
                    (policy.audit_days,),
                )
                deleted_audit_count = cursor.rowcount
            connection.commit()
        else:
            deleted_job_count = 0
            deleted_audit_count = 0

        return {
            "apply": apply,
            "policy": {
                "raw_days": policy.raw_days,
                "derived_days": policy.derived_days,
                "audit_days": policy.audit_days,
            },
            "candidates": {
                "held_jobs": len(held_jobs),
                "raw_artifacts": len(raw_candidates),
                "jobs": len(expired_jobs),
                "audit_events": expired_audit_count,
            },
            "deleted": {
                "objects": deleted_objects,
                "jobs": deleted_job_count,
                "audit_events": deleted_audit_count,
            },
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply IDP artifact, metadata, and audit retention policy")
    parser.add_argument("--apply", action="store_true", help="Delete expired data; default is a dry run")
    args = parser.parse_args()
    try:
        result = enforce_retention(apply=args.apply)
    except Exception as exc:  # noqa: BLE001
        print(f"retention enforcement failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
