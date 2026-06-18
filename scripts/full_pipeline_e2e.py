#!/usr/bin/env python3
"""Run and validate the live IDP full-pipeline e2e flow.

The runner intentionally uses only the standard library so it can be used from a
fresh checkout, a CI job, or a lightweight Docker container.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


COMPLETED_STATUS = "COMPLETED"
FAILED_STATUSES = {"FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"}
VALID_FINAL_STATUSES = {"delivered", "pending_human_review"}
REQUIRED_STAGES = ("preprocess", "ocr", "layout", "classification", "extraction", "validation")
DEFAULT_LOG_SERVICES = (
    "ingestion-service",
    "workflow-orchestrator",
    "preprocess-worker",
    "ocr-service",
    "layout-service",
    "classifier-router-service",
    "extraction-service",
    "validation-service",
    "human-review-console",
    "delivery-service",
    "evaluation-service",
)


class E2EError(RuntimeError):
    """Raised when the e2e run fails a runtime or contract assertion."""


@dataclass(frozen=True)
class RunnerConfig:
    api_url: str
    sample_path: Path
    artifact_dir: Path
    timeout_seconds: int
    poll_interval_seconds: float
    ingestion_api_key: str
    tenant_id: str
    actor_id: str
    idempotency_key: str
    strict_required_fields: bool
    require_observability: bool
    prometheus_url: str
    collect_logs: bool
    log_tail: int


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise E2EError(f"{name} must be an integer") from exc


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise E2EError(f"{name} must be a number") from exc


def build_config(args: argparse.Namespace) -> RunnerConfig:
    sample_path = Path(args.sample_path or os.environ.get("SAMPLE_PATH", "samples/documents/sample_invoice_001.png"))
    run_id = os.environ.get("E2E_RUN_ID") or time.strftime("%Y%m%d-%H%M%S")
    default_artifact_dir = Path(os.environ.get("E2E_ARTIFACT_DIR", f"artifacts/e2e/{run_id}"))
    return RunnerConfig(
        api_url=(args.api_url or os.environ.get("API_URL", "http://localhost:8000")).rstrip("/"),
        sample_path=sample_path,
        artifact_dir=Path(args.artifact_dir or default_artifact_dir),
        timeout_seconds=args.timeout_seconds or env_int("TIMEOUT_SECONDS", 600),
        poll_interval_seconds=args.poll_interval or env_float("POLL_INTERVAL", 5.0),
        ingestion_api_key=args.ingestion_api_key or os.environ.get("INGESTION_API_KEY", "dev-ingestion-key"),
        tenant_id=args.tenant_id or os.environ.get("TENANT_ID", "default"),
        actor_id=args.actor_id or os.environ.get("ACTOR_ID", "e2e-runner"),
        idempotency_key=args.idempotency_key or os.environ.get("E2E_IDEMPOTENCY_KEY", f"e2e-{uuid.uuid4()}"),
        strict_required_fields=args.strict_required_fields or env_bool("STRICT_REQUIRED_FIELDS", False),
        require_observability=args.require_observability or env_bool("E2E_REQUIRE_OBSERVABILITY", False),
        prometheus_url=(args.prometheus_url or os.environ.get("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/"),
        collect_logs=not args.no_collect_logs and env_bool("E2E_COLLECT_LOGS", True),
        log_tail=args.log_tail or env_int("E2E_LOG_TAIL", 160),
    )


def redact_config(config: RunnerConfig) -> dict[str, Any]:
    data = asdict(config)
    data["sample_path"] = str(config.sample_path)
    data["artifact_dir"] = str(config.artifact_dir)
    data["ingestion_api_key"] = "***"
    return data


def auth_headers(config: RunnerConfig) -> dict[str, str]:
    return {
        "X-API-Key": config.ingestion_api_key,
        "X-Tenant-Id": config.tenant_id,
        "X-Actor-Id": config.actor_id,
    }


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    req = request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except error.HTTPError as exc:
        preview = exc.read().decode("utf-8", errors="replace")[:800]
        raise E2EError(f"{method} {url} failed with HTTP {exc.code}: {preview}") from exc
    except error.URLError as exc:
        raise E2EError(f"{method} {url} failed: {exc.reason}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise E2EError(f"{method} {url} returned non-JSON response: {raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise E2EError(f"{method} {url} returned JSON {type(data).__name__}, expected object")
    return data


def multipart_file_body(path: Path) -> tuple[bytes, str]:
    boundary = f"----idp-e2e-{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_bytes = path.read_bytes()
    parts = [
        f"--{boundary}\r\n".encode(),
        (
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode(),
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def ensure_sample_document(path: Path) -> None:
    if path.exists():
        return
    generator = Path("scripts/generate_sample_invoice.py")
    if not generator.exists():
        raise E2EError(f"sample file not found and generator is missing: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(generator), "--output", str(path)], check=True)


def submit_document(config: RunnerConfig) -> dict[str, Any]:
    body, content_type = multipart_file_body(config.sample_path)
    headers = {
        **auth_headers(config),
        "Idempotency-Key": config.idempotency_key,
        "Content-Type": content_type,
    }
    return http_json("POST", f"{config.api_url}/documents", headers=headers, body=body, timeout=60)


def get_status(config: RunnerConfig, job_id: str) -> dict[str, Any]:
    return http_json("GET", f"{config.api_url}/documents/{job_id}", headers=auth_headers(config), timeout=30)


def get_result(config: RunnerConfig, job_id: str) -> dict[str, Any]:
    return http_json("GET", f"{config.api_url}/documents/{job_id}/result", headers=auth_headers(config), timeout=60)


def poll_until_complete(config: RunnerConfig, job_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + config.timeout_seconds
    history: list[dict[str, Any]] = []
    last_status: dict[str, Any] = {}

    while time.monotonic() < deadline:
        last_status = get_status(config, job_id)
        status = str(last_status.get("workflow_status") or last_status.get("status") or "UNKNOWN")
        history.append(
            {
                "seen_at_unix": round(time.time(), 3),
                "status": status,
                "workflow_status": last_status.get("workflow_status"),
                "job_status": last_status.get("status"),
            }
        )
        print(f"workflow status: {status}", flush=True)

        if status == COMPLETED_STATUS:
            return last_status, history
        if status in FAILED_STATUSES:
            return last_status, history
        time.sleep(config.poll_interval_seconds)

    return last_status, history


def require_dict(payload: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        errors.append(f"missing_or_invalid_stage:{key}")
        return {}
    return value


def require_non_empty(payload: dict[str, Any], key: str, stage: str, errors: list[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{stage}:missing_{key}")


def require_number(payload: dict[str, Any], key: str, stage: str, errors: list[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, (int, float)):
        errors.append(f"{stage}:missing_or_invalid_{key}")


def validate_pipeline_result(payload: dict[str, Any], *, strict_required_fields: bool) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"workflow_final_status": "unknown"}, ["missing_result_object"]

    final_status = str(result.get("status") or "unknown")
    if final_status not in VALID_FINAL_STATUSES:
        errors.append(f"unexpected_final_status:{final_status}")

    for stage in REQUIRED_STAGES:
        require_dict(result, stage, errors)

    preprocess = require_dict(result, "preprocess", errors)
    ocr = require_dict(result, "ocr", errors)
    layout = require_dict(result, "layout", errors)
    classification = require_dict(result, "classification", errors)
    extraction = require_dict(result, "extraction", errors)
    validation = require_dict(result, "validation", errors)

    require_non_empty(preprocess, "preprocessed_key", "preprocess", errors)
    require_non_empty(ocr, "ocr_key", "ocr", errors)
    require_number(ocr, "mean_confidence", "ocr", errors)
    require_non_empty(layout, "layout_key", "layout", errors)
    require_non_empty(classification, "route", "classification", errors)
    require_non_empty(extraction, "extraction_key", "extraction", errors)
    require_number(extraction, "confidence", "extraction", errors)
    if not isinstance(extraction.get("fields"), dict):
        errors.append("extraction:missing_fields")
    if not isinstance(extraction.get("used_vlm_fallback"), bool):
        errors.append("extraction:missing_used_vlm_fallback")
    require_non_empty(validation, "verdict", "validation", errors)
    if not isinstance(validation.get("requires_human_review"), bool):
        errors.append("validation:missing_requires_human_review")

    fields = extraction.get("fields") if isinstance(extraction.get("fields"), dict) else {}
    route = str(extraction.get("route") or validation.get("route") or classification.get("route") or "unknown")
    required_invoice_fields = ("invoice_number", "invoice_date", "total_amount")
    missing_required_fields = [field for field in required_invoice_fields if not str(fields.get(field) or "").strip()]

    if final_status == "delivered":
        delivery = require_dict(result, "delivery", errors)
        if delivery.get("delivery_status") != "success":
            errors.append("delivery:status_not_success")
        require_non_empty(delivery, "delivery_id", "delivery", errors)
        require_non_empty(delivery, "delivery_receipt_key", "delivery", errors)
        if missing_required_fields:
            errors.append(f"delivered_with_missing_required_fields:{','.join(missing_required_fields)}")

    if final_status == "pending_human_review":
        review_task = require_dict(result, "review_task", errors)
        require_non_empty(review_task, "review_task_id", "review_task", errors)
        require_non_empty(review_task, "review_status", "review_task", errors)

    if strict_required_fields and missing_required_fields:
        errors.append(f"missing_required_fields:{','.join(missing_required_fields)}")

    summary = {
        "job_id": payload.get("job_id") or result.get("job_id"),
        "workflow_final_status": final_status,
        "route": route,
        "ocr_confidence": ocr.get("mean_confidence"),
        "ocr_fallback_used": bool(ocr.get("fallback_used")),
        "layout_block_count": layout.get("block_count"),
        "extraction_confidence": extraction.get("confidence"),
        "used_vlm_fallback": extraction.get("used_vlm_fallback"),
        "requires_review": validation.get("requires_human_review"),
        "validation_verdict": validation.get("verdict"),
        "missing_required_fields": missing_required_fields,
        "branch": "review" if final_status == "pending_human_review" else "delivery",
    }
    if isinstance(result.get("delivery"), dict):
        summary["delivery_id"] = result["delivery"].get("delivery_id")
    if isinstance(result.get("review_task"), dict):
        summary["review_task_id"] = result["review_task"].get("review_task_id")
        summary["review_provider"] = result["review_task"].get("provider")
    return summary, errors


def check_observability(config: RunnerConfig) -> dict[str, Any]:
    query = parse.urlencode({"query": 'up{tier="idp-pipeline"}'})
    data = http_json("GET", f"{config.prometheus_url}/api/v1/query?{query}", timeout=20)
    results = data.get("data", {}).get("result", []) if isinstance(data.get("data"), dict) else []
    up_targets = [
        item
        for item in results
        if isinstance(item, dict)
        and isinstance(item.get("value"), list)
        and len(item["value"]) >= 2
        and str(item["value"][1]) == "1"
    ]
    service_names = sorted(
        str(
            item.get("metric", {}).get("service")
            or item.get("metric", {}).get("instance")
            or item.get("metric", {}).get("job")
            or "unknown"
        )
        for item in up_targets
    )
    return {
        "target_count": len(results),
        "up_target_count": len(up_targets),
        "up_services": service_names,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_docker_logs(config: RunnerConfig, artifact_dir: Path) -> None:
    if not config.collect_logs:
        return
    logs_dir = artifact_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for service in DEFAULT_LOG_SERVICES:
        output_path = logs_dir / f"{service}.log"
        try:
            completed = subprocess.run(
                ["docker", "compose", "logs", f"--tail={config.log_tail}", service],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            output_path.write_text(f"failed to collect logs: {exc}\n", encoding="utf-8")


def run(config: RunnerConfig) -> dict[str, Any]:
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    write_json(config.artifact_dir / "config.json", redact_config(config))

    ensure_sample_document(config.sample_path)
    print(f"submitting document: {config.sample_path}", flush=True)
    submission = submit_document(config)
    write_json(config.artifact_dir / "submission.json", submission)

    job_id = str(submission.get("job_id") or "")
    if not job_id:
        raise E2EError("submission response did not include job_id")
    print(f"job_id={job_id} workflow_id={submission.get('workflow_id') or ''}", flush=True)

    try:
        final_status, status_history = poll_until_complete(config, job_id)
        write_json(config.artifact_dir / "status_history.json", status_history)
        write_json(config.artifact_dir / "final_status.json", final_status)
        status = str(final_status.get("workflow_status") or final_status.get("status") or "UNKNOWN")
        if status != COMPLETED_STATUS:
            raise E2EError(
                f"workflow ended unsuccessfully: {status}"
                if status in FAILED_STATUSES
                else f"timeout after {config.timeout_seconds}s waiting for workflow completion; last status={status}"
            )

        result = get_result(config, job_id)
        write_json(config.artifact_dir / "result.json", result)

        summary, errors = validate_pipeline_result(result, strict_required_fields=config.strict_required_fields)
        summary["artifact_dir"] = str(config.artifact_dir)
        summary["status_poll_count"] = len(status_history)
        summary["workflow_id"] = submission.get("workflow_id") or final_status.get("workflow_id")
        summary["workflow_run_id"] = submission.get("workflow_run_id") or final_status.get("workflow_run_id")

        if config.require_observability:
            observability = check_observability(config)
            summary["observability"] = observability
            if observability["up_target_count"] < 1:
                errors.append("observability:no_idp_pipeline_targets_up")

        write_json(config.artifact_dir / "summary.json", summary)
        if errors:
            write_json(config.artifact_dir / "validation_errors.json", errors)
            raise E2EError(f"pipeline contract validation failed: {', '.join(errors)}")
        return summary
    except Exception:
        collect_docker_logs(config, config.artifact_dir)
        raise


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live IDP full-pipeline e2e test")
    parser.add_argument("sample_path", nargs="?", help="Path to document file to submit")
    parser.add_argument("--api-url", help="Ingestion API base URL")
    parser.add_argument("--artifact-dir", help="Directory for e2e artifacts")
    parser.add_argument("--timeout-seconds", type=int, help="Workflow completion timeout")
    parser.add_argument("--poll-interval", type=float, help="Polling interval in seconds")
    parser.add_argument("--ingestion-api-key", help="Ingestion API key")
    parser.add_argument("--tenant-id", help="Tenant id")
    parser.add_argument("--actor-id", help="Actor id")
    parser.add_argument("--idempotency-key", help="Submission idempotency key")
    parser.add_argument("--strict-required-fields", action="store_true", help="Fail when invoice required fields are missing")
    parser.add_argument("--require-observability", action="store_true", help="Require Prometheus IDP targets to be up")
    parser.add_argument("--prometheus-url", help="Prometheus base URL")
    parser.add_argument("--no-collect-logs", action="store_true", help="Do not collect docker compose logs on failure")
    parser.add_argument("--log-tail", type=int, help="Number of log lines per service when collecting diagnostics")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        summary = run(build_config(args))
    except E2EError as exc:
        print(f"E2E failed: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"E2E failed: command exited {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        return 1

    print("E2E summary:")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("E2E test completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
