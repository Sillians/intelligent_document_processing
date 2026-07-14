#!/usr/bin/env python3
"""Run staging operational readiness drills and save evidence artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.full_pipeline_e2e import RunnerConfig, submit_document
from scripts.production_preflight import parse_env


DEFAULT_COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.staging.yml",
)
DEFAULT_STATEFUL_VOLUMES = (
    "postgres_data",
    "seaweedfs_data",
    "labelstudio_data",
    "grafana_data",
    "prometheus_data",
    "alertmanager_data",
)
REQUIRED_ALERTS = {
    "IDPServiceDown",
    "IDPHigh5xxRate",
    "IDPHighP95Latency",
    "IDPTemporalQueueBacklogHigh",
    "IDPTemporalQueueMetricsStale",
    "IDPTemporalQueueMetricsMissing",
    "IDPNodeExporterDown",
    "IDPDiskSpaceLow",
    "IDPDiskSpaceCritical",
}


class DrillError(RuntimeError):
    """Raised when an operational drill fails."""


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DrillError(f"{path} must contain a JSON object")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = True,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        preview = (completed.stderr or completed.stdout or "").strip()[:1200]
        raise DrillError(f"command failed ({completed.returncode}): {' '.join(cmd)}\n{preview}")
    return completed


def compose_base(args: argparse.Namespace) -> list[str]:
    cmd = ["docker", "compose", "--env-file", args.env_file]
    compose_files = args.compose_file or list(DEFAULT_COMPOSE_FILES)
    for compose_file in compose_files:
        cmd.extend(["-f", compose_file])
    return cmd


def compose_config(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    completed = run_command(compose_base(args) + ["config", "--format", "json"], cwd=repo_root)
    return json.loads(completed.stdout)


def compose_project_name(args: argparse.Namespace, repo_root: Path) -> str:
    name = str(compose_config(args, repo_root).get("name") or "").strip()
    if not name:
        raise DrillError("unable to determine Compose project name")
    return name


def evidence_dir(base: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path("artifacts") / "staging" / base / timestamp()


def smoke(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    artifact_dir = evidence_dir("smoke", args.artifact_dir)
    env = os.environ.copy()
    env.update(
        {
            "API_URL": args.api_url.rstrip("/"),
            "INGESTION_API_KEY": args.api_key,
            "TENANT_ID": args.tenant_id,
            "ACTOR_ID": args.actor_id,
            "E2E_ARTIFACT_DIR": str(artifact_dir),
            "E2E_RUN_ID": artifact_dir.name,
            "E2E_REQUIRE_OBSERVABILITY": "1" if args.require_observability else "0",
            "PROMETHEUS_URL": args.prometheus_url.rstrip("/"),
        }
    )
    cmd = [sys.executable, "scripts/full_pipeline_e2e.py"]
    if args.sample_path:
        cmd.append(args.sample_path)
    if args.timeout_seconds:
        cmd.extend(["--timeout-seconds", str(args.timeout_seconds)])
    if args.poll_interval:
        cmd.extend(["--poll-interval", str(args.poll_interval)])

    started = time.time()
    completed = run_command(cmd, cwd=repo_root, env=env, capture=True, check=False)
    manifest = {
        "drill": "staging_smoke",
        "started_at_unix": started,
        "finished_at_unix": time.time(),
        "api_url": args.api_url.rstrip("/"),
        "tenant_id": args.tenant_id,
        "artifact_dir": str(artifact_dir),
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "passed": completed.returncode == 0,
    }
    write_json(artifact_dir / "drill_manifest.json", manifest)
    if completed.returncode != 0:
        raise DrillError(f"staging smoke drill failed; see {artifact_dir}")
    return manifest


def backup(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    artifact_dir = evidence_dir("backup", args.artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    project_name = compose_project_name(args, repo_root)
    manifest: dict[str, Any] = {
        "drill": "staging_backup",
        "project_name": project_name,
        "env_file": args.env_file,
        "compose_files": args.compose_file,
        "artifact_dir": str(artifact_dir),
        "started_at_unix": time.time(),
        "files": {},
    }

    postgres_path = artifact_dir / "postgres.sql"
    completed = run_command(
        compose_base(args) + ["exec", "-T", "postgres", "sh", "-lc", 'pg_dumpall -U "$POSTGRES_USER"'],
        cwd=repo_root,
        capture=True,
        check=True,
        timeout=args.command_timeout,
    )
    postgres_path.write_text(completed.stdout, encoding="utf-8")
    manifest["files"]["postgres.sql"] = {"bytes": postgres_path.stat().st_size}

    for logical_volume in args.volume:
        volume_name = f"{project_name}_{logical_volume}"
        archive_name = f"{logical_volume}.tgz"
        archive_path = artifact_dir / archive_name
        run_command(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume_name}:/source:ro",
                "-v",
                f"{artifact_dir}:/backup",
                "busybox",
                "tar",
                "czf",
                f"/backup/{archive_name}",
                "-C",
                "/source",
                ".",
            ],
            cwd=repo_root,
            capture=True,
            check=True,
            timeout=args.command_timeout,
        )
        manifest["files"][archive_name] = {"volume": volume_name, "bytes": archive_path.stat().st_size}

    env_values = parse_env(Path(args.env_file))
    encryption_key = env_values.get("BACKUP_ENCRYPTION_KEY", "")
    encryption_enabled = env_values.get("BACKUP_ENCRYPTION_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    if encryption_enabled and len(encryption_key) < 32:
        raise DrillError("BACKUP_ENCRYPTION_KEY must contain at least 32 characters")
    if encryption_enabled:
        encryption_env = os.environ.copy()
        encryption_env["IDP_BACKUP_ENCRYPTION_KEY"] = encryption_key
        encrypted_files: dict[str, Any] = {}
        for source_name, metadata in list(manifest["files"].items()):
            source_path = artifact_dir / source_name
            encrypted_name = f"{source_name}.enc"
            encrypted_path = artifact_dir / encrypted_name
            run_command(
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-salt",
                    "-in",
                    str(source_path),
                    "-out",
                    str(encrypted_path),
                    "-pass",
                    "env:IDP_BACKUP_ENCRYPTION_KEY",
                ],
                cwd=repo_root,
                env=encryption_env,
                timeout=args.command_timeout,
            )
            source_path.unlink()
            encrypted_files[encrypted_name] = {
                **metadata,
                "bytes": encrypted_path.stat().st_size,
                "sha256": sha256_file(encrypted_path),
                "encrypted": True,
                "cipher": "aes-256-cbc-pbkdf2",
                "plaintext_name": source_name,
            }
        manifest["files"] = encrypted_files
    else:
        for file_name, metadata in manifest["files"].items():
            metadata["sha256"] = sha256_file(artifact_dir / file_name)
    manifest["encryption_enabled"] = encryption_enabled
    manifest["finished_at_unix"] = time.time()
    manifest["passed"] = True
    write_json(artifact_dir / "backup_manifest.json", manifest)
    return manifest


def restore_verify(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    backup_dir = Path(args.backup_dir).resolve()
    if not backup_dir.exists():
        raise DrillError(f"backup directory does not exist: {backup_dir}")

    manifest: dict[str, Any] = {
        "drill": "staging_restore_verify",
        "backup_dir": str(backup_dir),
        "started_at_unix": time.time(),
        "checks": {},
    }
    backup_manifest_path = backup_dir / "backup_manifest.json"
    backup_manifest = read_json_file(backup_manifest_path) if backup_manifest_path.exists() else {}
    for file_name, metadata in backup_manifest.get("files", {}).items():
        source_path = backup_dir / file_name
        expected_hash = metadata.get("sha256") if isinstance(metadata, dict) else None
        if expected_hash and (not source_path.exists() or sha256_file(source_path) != expected_hash):
            raise DrillError(f"backup checksum mismatch: {file_name}")

    env_values = parse_env(Path(args.env_file)) if args.env_file else {}
    encryption_key = env_values.get("BACKUP_ENCRYPTION_KEY", "")

    def materialize(path: Path, destination: Path) -> Path:
        if path.suffix != ".enc":
            return path
        if len(encryption_key) < 32:
            raise DrillError("encrypted backup verification requires BACKUP_ENCRYPTION_KEY")
        decrypt_env = os.environ.copy()
        decrypt_env["IDP_BACKUP_ENCRYPTION_KEY"] = encryption_key
        run_command(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-in",
                str(path),
                "-out",
                str(destination),
                "-pass",
                "env:IDP_BACKUP_ENCRYPTION_KEY",
            ],
            cwd=repo_root,
            env=decrypt_env,
            timeout=args.command_timeout,
        )
        return destination

    encrypted_postgres_path = backup_dir / "postgres.sql.enc"
    postgres_source = encrypted_postgres_path if encrypted_postgres_path.exists() else backup_dir / "postgres.sql"
    temporary_directory = tempfile.TemporaryDirectory()
    temporary_root = Path(temporary_directory.name)
    postgres_path = materialize(postgres_source, temporary_root / "postgres.sql")
    if not postgres_path.exists() or postgres_path.stat().st_size <= 0:
        raise DrillError(f"missing or empty postgres backup: {postgres_source}")
    postgres_preview = postgres_path.read_text(encoding="utf-8", errors="replace")[:4000]
    manifest["checks"]["postgres.sql"] = {
        "bytes": postgres_path.stat().st_size,
        "contains_database_dump_markers": "PostgreSQL database dump" in postgres_preview
        or "CREATE DATABASE" in postgres_preview
        or "CREATE ROLE" in postgres_preview,
    }

    archive_sources = sorted(backup_dir.glob("*.tgz")) + sorted(backup_dir.glob("*.tgz.enc"))
    for archive_source in archive_sources:
        archive_path = materialize(
            archive_source,
            temporary_root / archive_source.name.removesuffix(".enc"),
        )
        completed = run_command(
            ["tar", "tzf", str(archive_path)],
            cwd=repo_root,
            capture=True,
            check=True,
            timeout=args.command_timeout,
        )
        entries = [line for line in completed.stdout.splitlines() if line.strip()]
        manifest["checks"][archive_source.name] = {
            "bytes": archive_source.stat().st_size,
            "encrypted": archive_source.suffix == ".enc",
            "entry_count": len(entries),
            "sample_entries": entries[:20],
        }

    if len(manifest["checks"]) < 2:
        raise DrillError("restore verification needs postgres.sql and at least one volume archive")

    manifest["finished_at_unix"] = time.time()
    manifest["passed"] = True
    write_json(backup_dir / "restore_verify_manifest.json", manifest)
    temporary_directory.cleanup()
    return manifest


def rollback(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    artifact_dir = evidence_dir("rollback", args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["IDP_IMAGE_REGISTRY"] = args.image_registry
    env["IDP_IMAGE_TAG"] = args.image_tag
    env["IDP_ENV_FILE"] = args.env_file

    before = run_command(compose_base(args) + ["ps", "--format", "json"], cwd=repo_root, env=env, check=False)
    (artifact_dir / "compose_ps_before.jsonl").write_text(before.stdout, encoding="utf-8")

    run_command(compose_base(args) + ["pull", "ingestion-service", "workflow-orchestrator", "delivery-service"], cwd=repo_root, env=env, timeout=args.command_timeout)
    run_command(
        compose_base(args)
        + ["up", "-d", "--no-build", "--no-deps", "gateway", "ingestion-service", "workflow-orchestrator", "delivery-service"],
        cwd=repo_root,
        env=env,
        timeout=args.command_timeout,
    )
    after = run_command(compose_base(args) + ["ps", "--format", "json"], cwd=repo_root, env=env, check=False)
    (artifact_dir / "compose_ps_after.jsonl").write_text(after.stdout, encoding="utf-8")

    manifest = {
        "drill": "staging_rollback",
        "image_registry": args.image_registry,
        "image_tag": args.image_tag,
        "artifact_dir": str(artifact_dir),
        "smoke_ran": False,
        "passed": True,
    }
    if args.api_url and args.api_key:
        smoke_args = argparse.Namespace(
            api_url=args.api_url,
            api_key=args.api_key,
            tenant_id=args.tenant_id,
            actor_id="rollback-drill",
            prometheus_url=args.prometheus_url,
            require_observability=args.require_observability,
            sample_path=args.sample_path,
            artifact_dir=str(artifact_dir / "smoke"),
            timeout_seconds=args.timeout_seconds,
            poll_interval=args.poll_interval,
        )
        manifest["smoke"] = smoke(smoke_args, repo_root)
        manifest["smoke_ran"] = True
    write_json(artifact_dir / "rollback_manifest.json", manifest)
    return manifest


def http_get_json(url: str, timeout_seconds: int) -> Any:
    try:
        with request.urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.URLError as exc:
        raise DrillError(f"GET {url} failed: {exc}") from exc


def prometheus_query(base_url: str, query: str, timeout_seconds: int) -> list[dict[str, Any]]:
    query_string = parse.urlencode({"query": query})
    payload = http_get_json(f"{base_url.rstrip('/')}/api/v1/query?{query_string}", timeout_seconds)
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise DrillError(f"Prometheus query failed: {query}")
    data = payload.get("data")
    results = data.get("result") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise DrillError(f"Prometheus query returned an invalid result: {query}")
    return results


def observability(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    del repo_root
    artifact_dir = evidence_dir("observability", args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prometheus_url = args.prometheus_url.rstrip("/")
    deadline = time.monotonic() + args.wait_seconds
    last_error = ""

    while time.monotonic() < deadline:
        try:
            pipeline_targets = prometheus_query(
                prometheus_url,
                'up{tier="idp-pipeline"} == 1',
                args.http_timeout,
            )
            queue_targets = prometheus_query(
                prometheus_url,
                'up{job="idp-workflow-queue"} == 1',
                args.http_timeout,
            )
            disk_targets = prometheus_query(
                prometheus_url,
                'up{job="node-exporter"} == 1',
                args.http_timeout,
            )
            queue_metrics = prometheus_query(
                prometheus_url,
                "idp_temporal_task_queue_backlog",
                args.http_timeout,
            )
            disk_metrics = prometheus_query(
                prometheus_url,
                'node_filesystem_avail_bytes{mountpoint="/"}',
                args.http_timeout,
            )
            if (
                len(pipeline_targets) >= args.min_pipeline_targets
                and queue_targets
                and disk_targets
                and queue_metrics
                and disk_metrics
            ):
                break
            last_error = (
                f"pipeline={len(pipeline_targets)}/{args.min_pipeline_targets} "
                f"queue_target={len(queue_targets)} queue_metrics={len(queue_metrics)} "
                f"disk_target={len(disk_targets)} disk_metrics={len(disk_metrics)}"
            )
        except DrillError as exc:
            last_error = str(exc)
        time.sleep(args.poll_interval)
    else:
        raise DrillError(f"observability signals did not become ready: {last_error}")

    rules_payload = http_get_json(f"{prometheus_url}/api/v1/rules?type=alert", args.http_timeout)
    groups = rules_payload.get("data", {}).get("groups", []) if isinstance(rules_payload, dict) else []
    loaded_alerts = {
        str(rule.get("name"))
        for group in groups
        if isinstance(group, dict)
        for rule in group.get("rules", [])
        if isinstance(rule, dict) and rule.get("type") == "alerting"
    }
    missing_alerts = sorted(REQUIRED_ALERTS - loaded_alerts)
    if missing_alerts:
        raise DrillError(f"required Prometheus alerts are not loaded: {', '.join(missing_alerts)}")

    alertmanager_status = http_get_json(
        f"{args.alertmanager_url.rstrip('/')}/api/v2/status",
        args.http_timeout,
    )
    grafana_health = http_get_json(
        f"{args.grafana_url.rstrip('/')}/api/health",
        args.http_timeout,
    )
    if not isinstance(grafana_health, dict) or grafana_health.get("database") != "ok":
        raise DrillError("Grafana health check did not report database=ok")

    manifest = {
        "drill": "staging_observability",
        "prometheus_url": prometheus_url,
        "pipeline_target_count": len(pipeline_targets),
        "queue_target_count": len(queue_targets),
        "queue_series_count": len(queue_metrics),
        "disk_target_count": len(disk_targets),
        "disk_series_count": len(disk_metrics),
        "required_alerts": sorted(REQUIRED_ALERTS),
        "loaded_alerts": sorted(loaded_alerts),
        "alertmanager_status": alertmanager_status,
        "grafana_health": grafana_health,
        "passed": True,
    }
    write_json(artifact_dir / "observability_manifest.json", manifest)
    write_json(artifact_dir / "prometheus_rules.json", rules_payload)
    return manifest


def request_status(url: str, api_key: str, tenant_id: str, timeout_seconds: int) -> int:
    req = request.Request(
        url,
        headers={
            "X-API-Key": api_key,
            "X-Tenant-Id": tenant_id,
            "X-Actor-Id": "staging-hardening",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return response.status
    except error.HTTPError as exc:
        return exc.code
    except error.URLError as exc:
        raise DrillError(f"GET {url} failed: {exc}") from exc


def hardening(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    artifact_dir = evidence_dir("hardening", args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    parsed_url = parse.urlparse(args.api_url)
    loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    transport = "https"
    if parsed_url.scheme != "https":
        if not (args.allow_loopback_http and parsed_url.hostname in loopback_hosts):
            raise DrillError("hardening requires HTTPS; only explicit loopback HTTP can be waived")
        transport = "loopback-http-waiver"
    if args.primary_tenant == args.secondary_tenant:
        raise DrillError("tenant isolation requires two different tenant IDs")
    if args.primary_api_key == args.secondary_api_key:
        raise DrillError("tenant isolation requires two different API keys")

    sample_path = Path(args.sample_path)
    if not sample_path.exists():
        raise DrillError(f"sample document not found: {sample_path}")
    runner_config = RunnerConfig(
        api_url=args.api_url.rstrip("/"),
        sample_path=sample_path,
        artifact_dir=artifact_dir,
        timeout_seconds=args.http_timeout,
        poll_interval_seconds=1,
        ingestion_api_key=args.primary_api_key,
        tenant_id=args.primary_tenant,
        actor_id="staging-hardening",
        idempotency_key=f"hardening:{uuid.uuid4()}",
        strict_required_fields=False,
        require_observability=False,
        prometheus_url="",
        collect_logs=False,
        log_tail=0,
    )
    submission = submit_document(runner_config)
    write_json(artifact_dir / "submission.json", submission)
    job_id = str(submission.get("job_id") or "")
    if not job_id or not all(character.isalnum() or character == "-" for character in job_id):
        raise DrillError("hardening submission returned an invalid job_id")

    document_url = f"{args.api_url.rstrip('/')}/documents/{job_id}"
    checks = {
        "invalid_api_key_status": request_status(
            document_url,
            f"invalid-{uuid.uuid4()}",
            args.primary_tenant,
            args.http_timeout,
        ),
        "mismatched_tenant_header_status": request_status(
            document_url,
            args.primary_api_key,
            args.secondary_tenant,
            args.http_timeout,
        ),
        "cross_tenant_lookup_status": request_status(
            document_url,
            args.secondary_api_key,
            args.secondary_tenant,
            args.http_timeout,
        ),
        "public_audit_endpoint_status": request_status(
            f"{document_url}/audit",
            args.primary_api_key,
            args.primary_tenant,
            args.http_timeout,
        ),
    }
    expected_statuses = {
        "invalid_api_key_status": 401,
        "mismatched_tenant_header_status": 403,
        "cross_tenant_lookup_status": 404,
        "public_audit_endpoint_status": 404,
    }
    failures = [
        f"{name} expected {expected}, received {checks[name]}"
        for name, expected in expected_statuses.items()
        if checks[name] != expected
    ]

    env_values = parse_env(Path(args.env_file))
    postgres_user = env_values.get("POSTGRES_USER", "idp")
    postgres_db = env_values.get("POSTGRES_DB", "idp")
    audit_query = (
        "SELECT COUNT(*) FROM ingestion_audit_events "
        f"WHERE job_id = '{job_id}' AND tenant_id = '{args.primary_tenant}';"
    )
    if not all(character.isalnum() or character in {"-", "_"} for character in args.primary_tenant):
        raise DrillError("tenant IDs used by the hardening drill must be alphanumeric with hyphen or underscore")
    audit_result = run_command(
        compose_base(args)
        + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            postgres_user,
            "-d",
            postgres_db,
            "-tAc",
            audit_query,
        ],
        cwd=repo_root,
        timeout=args.command_timeout,
    )
    try:
        audit_event_count = int(audit_result.stdout.strip())
    except ValueError as exc:
        raise DrillError("unable to parse audit event count from Postgres") from exc
    if audit_event_count < 1:
        failures.append("no audit event was persisted for the hardening submission")
    if failures:
        raise DrillError("; ".join(failures))

    manifest = {
        "drill": "staging_hardening",
        "job_id": job_id,
        "primary_tenant": args.primary_tenant,
        "secondary_tenant": args.secondary_tenant,
        "transport": transport,
        "checks": checks,
        "audit_event_count": audit_event_count,
        "passed": True,
    }
    write_json(artifact_dir / "hardening_manifest.json", manifest)
    return manifest


def alert(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    if not args.confirm:
        raise DrillError("alert drill requires --confirm because it stops a staging service")

    artifact_dir = evidence_dir("alert", args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "drill": "staging_alert",
        "service": args.service,
        "alert_name": args.alert_name,
        "artifact_dir": str(artifact_dir),
        "started_at_unix": time.time(),
    }

    try:
        before = run_command(compose_base(args) + ["ps", "--format", "json"], cwd=repo_root, check=False)
        (artifact_dir / "compose_ps_before.jsonl").write_text(before.stdout, encoding="utf-8")
        run_command(compose_base(args) + ["stop", args.service], cwd=repo_root, timeout=args.command_timeout)
        time.sleep(args.wait_seconds)
        alerts = http_get_json(f"{args.alertmanager_url.rstrip('/')}/api/v2/alerts", args.http_timeout)
        write_json(artifact_dir / "alertmanager_alerts.json", alerts)
        matching = [
            item
            for item in alerts
            if isinstance(item, dict)
            and item.get("labels", {}).get("alertname") == args.alert_name
            and item.get("status", {}).get("state") in {"active", "unprocessed", "suppressed"}
        ]
        manifest["matching_alert_count"] = len(matching)
        manifest["matched_alerts"] = matching
        if not matching:
            raise DrillError(f"no active {args.alert_name} alert observed after stopping {args.service}")
    finally:
        run_command(compose_base(args) + ["up", "-d", args.service], cwd=repo_root, check=False, timeout=args.command_timeout)

    after = run_command(compose_base(args) + ["ps", "--format", "json"], cwd=repo_root, check=False)
    (artifact_dir / "compose_ps_after.jsonl").write_text(after.stdout, encoding="utf-8")
    manifest["finished_at_unix"] = time.time()
    manifest["passed"] = True
    write_json(artifact_dir / "alert_drill_manifest.json", manifest)
    return manifest


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", default=".env.staging")
    parser.add_argument("--compose-file", action="append")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--command-timeout", type=int, default=600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run staging operational readiness drills")
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--api-url", required=True)
    smoke_parser.add_argument("--api-key", required=True)
    smoke_parser.add_argument("--tenant-id", default="default")
    smoke_parser.add_argument("--actor-id", default="staging-smoke")
    smoke_parser.add_argument("--prometheus-url", default="http://localhost:9090")
    smoke_parser.add_argument("--require-observability", action="store_true")
    smoke_parser.add_argument("--sample-path")
    smoke_parser.add_argument("--timeout-seconds", type=int, default=900)
    smoke_parser.add_argument("--poll-interval", type=float, default=5.0)
    smoke_parser.add_argument("--artifact-dir")

    backup_parser = subparsers.add_parser("backup")
    add_common(backup_parser)
    backup_parser.add_argument("--volume", action="append", default=list(DEFAULT_STATEFUL_VOLUMES))

    restore_parser = subparsers.add_parser("restore-verify")
    restore_parser.add_argument("--backup-dir", required=True)
    restore_parser.add_argument("--env-file")
    restore_parser.add_argument("--command-timeout", type=int, default=300)

    rollback_parser = subparsers.add_parser("rollback")
    add_common(rollback_parser)
    rollback_parser.add_argument("--image-registry", required=True)
    rollback_parser.add_argument("--image-tag", required=True)
    rollback_parser.add_argument("--api-url")
    rollback_parser.add_argument("--api-key")
    rollback_parser.add_argument("--tenant-id", default="default")
    rollback_parser.add_argument("--prometheus-url", default="http://localhost:9090")
    rollback_parser.add_argument("--require-observability", action="store_true")
    rollback_parser.add_argument("--sample-path")
    rollback_parser.add_argument("--timeout-seconds", type=int, default=900)
    rollback_parser.add_argument("--poll-interval", type=float, default=5.0)

    alert_parser = subparsers.add_parser("alert")
    add_common(alert_parser)
    alert_parser.add_argument("--service", default="delivery-service")
    alert_parser.add_argument("--alertmanager-url", default="http://localhost:9093")
    alert_parser.add_argument("--alert-name", default="IDPServiceDown")
    alert_parser.add_argument("--wait-seconds", type=int, default=150)
    alert_parser.add_argument("--http-timeout", type=int, default=20)
    alert_parser.add_argument("--confirm", action="store_true")

    observability_parser = subparsers.add_parser("observability")
    observability_parser.add_argument("--prometheus-url", default="http://localhost:9090")
    observability_parser.add_argument("--alertmanager-url", default="http://localhost:9093")
    observability_parser.add_argument("--grafana-url", default="http://localhost:3000")
    observability_parser.add_argument("--min-pipeline-targets", type=int, default=10)
    observability_parser.add_argument("--wait-seconds", type=int, default=180)
    observability_parser.add_argument("--poll-interval", type=float, default=5.0)
    observability_parser.add_argument("--http-timeout", type=int, default=20)
    observability_parser.add_argument("--artifact-dir")

    hardening_parser = subparsers.add_parser("hardening")
    add_common(hardening_parser)
    hardening_parser.add_argument("--api-url", required=True)
    hardening_parser.add_argument("--primary-api-key", required=True)
    hardening_parser.add_argument("--primary-tenant", required=True)
    hardening_parser.add_argument("--secondary-api-key", required=True)
    hardening_parser.add_argument("--secondary-tenant", required=True)
    hardening_parser.add_argument("--sample-path", default="samples/documents/sample_invoice_001.png")
    hardening_parser.add_argument("--http-timeout", type=int, default=30)
    hardening_parser.add_argument("--allow-loopback-http", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    args = build_parser().parse_args(argv or sys.argv[1:])
    try:
        if args.command == "smoke":
            result = smoke(args, repo_root)
        elif args.command == "backup":
            result = backup(args, repo_root)
        elif args.command == "restore-verify":
            result = restore_verify(args, repo_root)
        elif args.command == "rollback":
            result = rollback(args, repo_root)
        elif args.command == "alert":
            result = alert(args, repo_root)
        elif args.command == "observability":
            result = observability(args, repo_root)
        elif args.command == "hardening":
            result = hardening(args, repo_root)
        else:
            raise DrillError(f"unknown command: {args.command}")
    except DrillError as exc:
        print(f"staging drill failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
