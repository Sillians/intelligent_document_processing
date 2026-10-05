#!/usr/bin/env python3
"""Record evidence-backed service boundary recommendations from a benchmark run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def assess(summary: dict[str, Any], *, minimum_samples: int = 20) -> dict[str, Any]:
    metrics = summary.get("metrics") or {}
    gate = summary.get("quality_gate") or {}
    count = int(metrics.get("sample_count") or 0)
    stage_p95 = metrics.get("stage_p95_ms") or {}
    representative = bool(gate.get("passed")) and count >= minimum_samples and int(metrics.get("document_family_count") or 0) >= 3 and int(metrics.get("multi_page_sample_count") or 0) >= 5
    decisions = {
        "ingestion_service": "keep: owns tenant auth, durable intake, and outbox",
        "workflow_orchestrator": "keep: owns durable multi-day review and retries",
        "ocr_service": "keep: independently scalable CPU/GPU engine",
        "human_review_console": "keep: reviewer identity and decision durability",
        "delivery_service": "keep: external side effects and replay protection",
    }
    for name, stage in (("classifier_router_service", "classification"), ("validation_service", "validation")):
        value = stage_p95.get(stage)
        if not representative or not isinstance(value, (int, float)):
            decisions[name] = "defer: representative quality and stage-latency evidence required"
        elif value <= 10:
            decisions[name] = "candidate_to_combine: low measured compute p95; measure network/queue overhead before changing deployment"
        else:
            decisions[name] = "keep_pending_profiling: measured compute p95 exceeds lightweight-stage threshold"
    for name in ("preprocess_worker", "layout_service", "extraction_service"):
        decisions[name] = "keep_pending_profiling" if representative else "defer: representative quality evidence required"
    return {
        "representative_gate_passed": representative,
        "sample_count": count,
        "stage_p95_ms": stage_p95,
        "decisions": decisions,
        "caveat": "Stage duration excludes transport, object-store, Temporal queue, and idle time; candidates are not automatic merge approvals.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = assess(json.loads(args.summary.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
