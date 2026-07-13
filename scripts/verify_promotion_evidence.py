#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
THRESHOLD_METRICS = {
    "min_sample_count": "sample_count",
    "min_completion_rate": "completion_rate",
    "min_contract_pass_rate": "pipeline_contract_pass_rate",
    "min_route_accuracy": "route_accuracy",
    "min_field_f1": "field_f1_mean",
    "min_validation_accuracy": "validation_accuracy",
    "min_throughput_documents_per_minute": "throughput_documents_per_minute",
    "max_human_review_rate": "human_review_rate",
    "max_ocr_cer": "ocr_cer_mean",
    "max_ocr_wer": "ocr_wer_mean",
    "max_p95_latency_seconds": "latency_p95_seconds",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def verify_evidence(artifact_dir: Path, expected_sha: str, policy_path: Path) -> dict[str, Any]:
    failures: list[str] = []
    if not SHA_PATTERN.fullmatch(expected_sha):
        failures.append("expected release SHA must contain exactly 40 lowercase hexadecimal characters")

    config = read_json(artifact_dir / "config.json")
    summary = read_json(artifact_dir / "summary.json")
    gate = read_json(artifact_dir / "quality_gate.json")
    policy = read_json(policy_path)

    if policy.get("status") != "approved":
        failures.append("release acceptance policy status must be approved")
    if config.get("pipeline_version") != expected_sha:
        failures.append("benchmark pipeline_version does not match the requested release SHA")
    if not gate.get("enabled"):
        failures.append("benchmark quality gate was not enabled")
    if gate.get("passed") is not True:
        failures.append("benchmark quality gate did not pass")
    if summary.get("quality_gate", {}).get("passed") is not True:
        failures.append("benchmark summary does not report a passing quality gate")
    if summary.get("error_count") != 0:
        failures.append("benchmark summary contains execution errors")

    policy_thresholds = policy.get("thresholds")
    if not isinstance(policy_thresholds, dict):
        failures.append("release acceptance policy must contain thresholds")
        policy_thresholds = {}
    actual_checks = {
        str(check.get("metric")): check
        for check in gate.get("checks", [])
        if isinstance(check, dict) and check.get("metric")
    }
    for policy_key, metric_name in THRESHOLD_METRICS.items():
        expected_threshold = policy_thresholds.get(policy_key)
        check = actual_checks.get(metric_name)
        if expected_threshold is None:
            failures.append(f"release acceptance policy is missing {policy_key}")
        elif check is None:
            failures.append(f"benchmark gate is missing {metric_name}")
        else:
            try:
                threshold_matches = float(check.get("threshold")) == float(expected_threshold)
            except (TypeError, ValueError):
                threshold_matches = False
            if not threshold_matches:
                failures.append(f"benchmark threshold for {metric_name} does not match the approved policy")
            elif check.get("passed") is not True:
                failures.append(f"benchmark check failed: {metric_name}")

    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    sample_count = int(metrics.get("sample_count") or 0)
    minimum_samples = int(policy_thresholds.get("min_sample_count") or 0)
    if sample_count < minimum_samples:
        failures.append(f"benchmark sample_count={sample_count} is below required {minimum_samples}")

    if failures:
        raise ValueError("; ".join(failures))
    return {
        "release_sha": expected_sha,
        "policy_profile": policy.get("profile"),
        "policy_status": policy.get("status"),
        "sample_count": sample_count,
        "quality_gate_passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify staging benchmark evidence before production promotion")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--policy", type=Path, default=Path("infra/staging/release_acceptance.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        receipt = verify_evidence(args.artifact_dir, args.expected_sha, args.policy)
    except ValueError as exc:
        print(f"promotion evidence verification failed: {exc}", file=sys.stderr)
        return 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
