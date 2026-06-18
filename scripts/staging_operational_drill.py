#!/usr/bin/env python3
"""Run staging operational readiness drills and save evidence artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


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


class DrillError(RuntimeError):
    """Raised when an operational drill fails."""


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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

    postgres_path = backup_dir / "postgres.sql"
    if not postgres_path.exists() or postgres_path.stat().st_size <= 0:
        raise DrillError(f"missing or empty postgres backup: {postgres_path}")
    postgres_preview = postgres_path.read_text(encoding="utf-8", errors="replace")[:4000]
    manifest["checks"]["postgres.sql"] = {
        "bytes": postgres_path.stat().st_size,
        "contains_database_dump_markers": "PostgreSQL database dump" in postgres_preview
        or "CREATE DATABASE" in postgres_preview
        or "CREATE ROLE" in postgres_preview,
    }

    for archive_path in sorted(backup_dir.glob("*.tgz")):
        completed = run_command(
            ["tar", "tzf", str(archive_path)],
            cwd=repo_root,
            capture=True,
            check=True,
            timeout=args.command_timeout,
        )
        entries = [line for line in completed.stdout.splitlines() if line.strip()]
        manifest["checks"][archive_path.name] = {
            "bytes": archive_path.stat().st_size,
            "entry_count": len(entries),
            "sample_entries": entries[:20],
        }

    if len(manifest["checks"]) < 2:
        raise DrillError("restore verification needs postgres.sql and at least one volume archive")

    manifest["finished_at_unix"] = time.time()
    manifest["passed"] = True
    write_json(backup_dir / "restore_verify_manifest.json", manifest)
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
        else:
            raise DrillError(f"unknown command: {args.command}")
    except DrillError as exc:
        print(f"staging drill failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
