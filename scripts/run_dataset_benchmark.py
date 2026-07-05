#!/usr/bin/env python3
"""Run dataset-backed IDP benchmarks against the live pipeline.

Supported sources:
- Hugging Face CORD-v2 via `--dataset cord-v2`
- Local JSONL manifests via `--manifest`

The runner submits each sample through the same ingestion and Temporal workflow
path as the smoke e2e test, then compares the final extraction fields against
dataset ground truth and optionally tracks aggregate metrics in evaluation-service.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import random
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.full_pipeline_e2e import (
    COMPLETED_STATUS,
    E2EError,
    RunnerConfig,
    get_result,
    http_json,
    poll_until_complete,
    submit_document,
    validate_pipeline_result,
    write_json,
)


CORD_DATASET_ID = "naver-clova-ix/cord-v2"
CORD_DEFAULT_SPLIT = "validation"
SUPPORTED_DATASETS = {"cord-v2"}
AMOUNT_KEYS = {
    "total_amount",
    "tax_amount",
    "subtotal_amount",
    "cash_amount",
    "change_amount",
}


@dataclass(frozen=True)
class BenchmarkConfig:
    dataset: str
    split: str
    manifest: Path | None
    limit: int
    offset: int
    shuffle: bool
    seed: int
    api_url: str
    evaluation_url: str
    artifact_dir: Path
    timeout_seconds: int
    poll_interval_seconds: float
    ingestion_api_key: str
    tenant_id: str
    actor_id: str
    pipeline_version: str
    track_evaluation: bool
    dry_run: bool
    concurrency: int
    thresholds_file: Path | None
    min_sample_count: int | None
    min_completion_rate: float | None
    min_contract_pass_rate: float | None
    min_route_accuracy: float | None
    min_field_f1: float | None
    min_validation_accuracy: float | None
    min_throughput_documents_per_minute: float | None
    max_human_review_rate: float | None
    max_ocr_cer: float | None
    max_ocr_wer: float | None
    max_p95_latency_seconds: float | None


@dataclass(frozen=True)
class BenchmarkSample:
    sample_id: str
    dataset: str
    split: str
    image: Any
    ground_truth: dict[str, Any]
    expected_fields: dict[str, Any]
    expected_route: str
    metadata: dict[str, Any]
    expected_ocr_text: str = ""
    expected_validation_verdict: str = ""


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


def env_optional_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise E2EError(f"{name} must be a number") from exc


def env_optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise E2EError(f"{name} must be an integer") from exc


def validate_rate(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if not 0 <= value <= 1:
        raise E2EError(f"{name} must be between 0 and 1")
    return value


def validate_non_negative(name: str, value: float | None) -> float | None:
    if value is not None and value < 0:
        raise E2EError(f"{name} must be zero or greater")
    return value


def load_thresholds(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise E2EError(f"unable to load threshold file {path}: {exc}") from exc
    thresholds = payload.get("thresholds") if isinstance(payload, dict) else None
    if not isinstance(thresholds, dict):
        raise E2EError(f"threshold file {path} must contain a thresholds object")
    result: dict[str, float] = {}
    for key, value in thresholds.items():
        if value is not None:
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError) as exc:
                raise E2EError(f"threshold {key} in {path} must be numeric") from exc
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run IDP benchmark samples from public or local datasets")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", choices=sorted(SUPPORTED_DATASETS), help="Public dataset adapter to use")
    source.add_argument("--manifest", type=Path, help="Local JSONL manifest with image_path and ground_truth")
    parser.add_argument("--split", default=os.environ.get("BENCHMARK_SPLIT", CORD_DEFAULT_SPLIT))
    parser.add_argument("--limit", type=int, default=env_int("BENCHMARK_LIMIT", 10))
    parser.add_argument("--offset", type=int, default=env_int("BENCHMARK_OFFSET", 0))
    parser.add_argument("--shuffle", action="store_true", default=env_bool("BENCHMARK_SHUFFLE", False))
    parser.add_argument("--seed", type=int, default=env_int("BENCHMARK_SEED", 1337))
    parser.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8000"))
    parser.add_argument("--evaluation-url", default=os.environ.get("EVALUATION_URL", "http://localhost:8018"))
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=env_int("TIMEOUT_SECONDS", 900))
    parser.add_argument("--poll-interval", type=float, default=env_float("POLL_INTERVAL", 5.0))
    parser.add_argument("--ingestion-api-key", default=os.environ.get("INGESTION_API_KEY", "dev-ingestion-key"))
    parser.add_argument("--tenant-id", default=os.environ.get("TENANT_ID", "default"))
    parser.add_argument("--actor-id", default=os.environ.get("ACTOR_ID", "benchmark-runner"))
    parser.add_argument("--pipeline-version", default=os.environ.get("PIPELINE_VERSION", "development"))
    parser.add_argument("--concurrency", type=int, default=env_int("BENCHMARK_CONCURRENCY", 1))
    parser.add_argument(
        "--thresholds-file",
        type=Path,
        default=Path(os.environ["BENCHMARK_THRESHOLDS_FILE"]) if os.environ.get("BENCHMARK_THRESHOLDS_FILE") else None,
    )
    parser.add_argument("--no-track-evaluation", action="store_true", help="Do not POST aggregate metrics to evaluation-service")
    parser.add_argument("--dry-run", action="store_true", help="Load samples and write expected manifests without submitting")
    parser.add_argument("--min-sample-count", type=int, default=env_optional_int("MIN_SAMPLE_COUNT"))
    parser.add_argument("--min-completion-rate", type=float, default=env_optional_float("MIN_COMPLETION_RATE"))
    parser.add_argument("--min-contract-pass-rate", type=float, default=env_optional_float("MIN_CONTRACT_PASS_RATE"))
    parser.add_argument("--min-route-accuracy", type=float, default=env_optional_float("MIN_ROUTE_ACCURACY"))
    parser.add_argument("--min-field-f1", type=float, default=env_optional_float("MIN_FIELD_F1"))
    parser.add_argument("--min-validation-accuracy", type=float, default=env_optional_float("MIN_VALIDATION_ACCURACY"))
    parser.add_argument(
        "--min-throughput-documents-per-minute",
        type=float,
        default=env_optional_float("MIN_THROUGHPUT_DOCUMENTS_PER_MINUTE"),
    )
    parser.add_argument("--max-human-review-rate", type=float, default=env_optional_float("MAX_HUMAN_REVIEW_RATE"))
    parser.add_argument("--max-ocr-cer", type=float, default=env_optional_float("MAX_OCR_CER"))
    parser.add_argument("--max-ocr-wer", type=float, default=env_optional_float("MAX_OCR_WER"))
    parser.add_argument("--max-p95-latency-seconds", type=float, default=env_optional_float("MAX_P95_LATENCY_SECONDS"))
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> BenchmarkConfig:
    dataset = args.dataset or "manifest"
    run_id = os.environ.get("BENCHMARK_RUN_ID") or time.strftime("%Y%m%d-%H%M%S")
    artifact_dir = args.artifact_dir or Path(f"artifacts/benchmarks/{dataset}-{args.split}-{run_id}")
    thresholds = load_thresholds(args.thresholds_file)

    def threshold(name: str) -> float | None:
        value = getattr(args, name)
        return value if value is not None else thresholds.get(name)

    return BenchmarkConfig(
        dataset=dataset,
        split=args.split,
        manifest=args.manifest,
        limit=max(1, args.limit),
        offset=max(0, args.offset),
        shuffle=bool(args.shuffle),
        seed=args.seed,
        api_url=args.api_url.rstrip("/"),
        evaluation_url=args.evaluation_url.rstrip("/"),
        artifact_dir=artifact_dir,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval,
        ingestion_api_key=args.ingestion_api_key,
        tenant_id=args.tenant_id,
        actor_id=args.actor_id,
        pipeline_version=args.pipeline_version,
        track_evaluation=not args.no_track_evaluation,
        dry_run=bool(args.dry_run),
        concurrency=max(1, args.concurrency),
        thresholds_file=args.thresholds_file,
        min_sample_count=(
            max(1, int(threshold("min_sample_count"))) if threshold("min_sample_count") is not None else None
        ),
        min_completion_rate=validate_rate("--min-completion-rate", threshold("min_completion_rate")),
        min_contract_pass_rate=validate_rate("--min-contract-pass-rate", threshold("min_contract_pass_rate")),
        min_route_accuracy=validate_rate("--min-route-accuracy", threshold("min_route_accuracy")),
        min_field_f1=validate_rate("--min-field-f1", threshold("min_field_f1")),
        min_validation_accuracy=validate_rate(
            "--min-validation-accuracy",
            threshold("min_validation_accuracy"),
        ),
        min_throughput_documents_per_minute=validate_non_negative(
            "--min-throughput-documents-per-minute",
            threshold("min_throughput_documents_per_minute"),
        ),
        max_human_review_rate=validate_rate("--max-human-review-rate", threshold("max_human_review_rate")),
        max_ocr_cer=validate_non_negative("--max-ocr-cer", threshold("max_ocr_cer")),
        max_ocr_wer=validate_non_negative("--max-ocr-wer", threshold("max_ocr_wer")),
        max_p95_latency_seconds=validate_non_negative(
            "--max-p95-latency-seconds",
            threshold("max_p95_latency_seconds"),
        ),
    )


def redacted_config(config: BenchmarkConfig) -> dict[str, Any]:
    data = asdict(config)
    data["artifact_dir"] = str(config.artifact_dir)
    data["manifest"] = str(config.manifest) if config.manifest else None
    data["thresholds_file"] = str(config.thresholds_file) if config.thresholds_file else None
    data["ingestion_api_key"] = "***"
    return data


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def parse_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise E2EError("ground truth must be a JSON object or JSON object string")


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def edit_distance(expected: list[str], predicted: list[str]) -> int:
    previous = list(range(len(predicted) + 1))
    for expected_index, expected_value in enumerate(expected, start=1):
        current = [expected_index]
        for predicted_index, predicted_value in enumerate(predicted, start=1):
            substitution_cost = 0 if expected_value == predicted_value else 1
            current.append(
                min(
                    current[-1] + 1,
                    previous[predicted_index] + 1,
                    previous[predicted_index - 1] + substitution_cost,
                )
            )
        previous = current
    return previous[-1]


def error_rate(expected_text: str, predicted_text: str, *, words: bool) -> float | None:
    expected_normalized = normalize_text(expected_text)
    if not expected_normalized:
        return None
    predicted_normalized = normalize_text(predicted_text)
    expected_units = expected_normalized.split() if words else list(expected_normalized)
    predicted_units = predicted_normalized.split() if words else list(predicted_normalized)
    return round(edit_distance(expected_units, predicted_units) / len(expected_units), 6)


def extract_cord_ocr_text(ground_truth: dict[str, Any]) -> str:
    lines: list[str] = []
    for line in as_list(ground_truth.get("valid_line")):
        if not isinstance(line, dict):
            continue
        words = [
            str(word.get("text") or "").strip()
            for word in as_list(line.get("words"))
            if isinstance(word, dict) and str(word.get("text") or "").strip()
        ]
        if words:
            lines.append(" ".join(words))
    return "\n".join(lines)


def normalize_amount(value: Any) -> str:
    text = str(value or "")
    # CORD uses mixed thousands separators and currency prefixes. Keep digits
    # and decimal-like separators, then normalize common Indonesian formats.
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return ""
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) == 3:
            text = "".join(parts)
        else:
            text = ".".join(parts)
    elif "." in text:
        parts = text.split(".")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            text = "".join(parts)
    try:
        parsed = float(text)
    except ValueError:
        return text
    if parsed.is_integer():
        return str(int(parsed))
    return f"{parsed:.2f}".rstrip("0").rstrip(".")


def normalize_field(key: str, value: Any) -> str:
    if key in AMOUNT_KEYS or key.endswith("_amount") or key.endswith("_price"):
        return normalize_amount(value)
    return normalize_text(value)


def extract_cord_ground_truth(raw_ground_truth: Any) -> dict[str, Any]:
    ground_truth = parse_jsonish(raw_ground_truth)
    gt_parse = ground_truth.get("gt_parse")
    if not isinstance(gt_parse, dict):
        raise E2EError("CORD ground_truth is missing gt_parse object")

    total = gt_parse.get("total") if isinstance(gt_parse.get("total"), dict) else {}
    sub_total = gt_parse.get("sub_total") if isinstance(gt_parse.get("sub_total"), dict) else {}
    menu_items = as_list(gt_parse.get("menu"))
    menu_items = [item for item in menu_items if isinstance(item, dict)]

    expected_fields = {
        "total_amount": first_non_empty(total.get("total_price"), total.get("total"), gt_parse.get("total_price")),
        "tax_amount": first_non_empty(sub_total.get("tax_price"), sub_total.get("tax")),
        "subtotal_amount": first_non_empty(sub_total.get("subtotal_price"), sub_total.get("subtotal")),
        "cash_amount": first_non_empty(total.get("cashprice"), total.get("cash_price")),
        "change_amount": first_non_empty(total.get("changeprice"), total.get("change_price")),
        "line_item_count": len(menu_items),
    }
    expected_fields = {key: value for key, value in expected_fields.items() if value not in {"", None, 0}}
    return {
        "expected_route": "receipt",
        "expected_fields": expected_fields,
        "gt_parse": gt_parse,
        "expected_ocr_text": extract_cord_ocr_text(ground_truth),
    }


def manifest_ground_truth(raw_ground_truth: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    ground_truth = parse_jsonish(raw_ground_truth)
    expected_route = str(ground_truth.get("expected_route") or ground_truth.get("route") or "generic")
    expected_fields = ground_truth.get("expected_fields") or ground_truth.get("fields") or {}
    if not isinstance(expected_fields, dict):
        raise E2EError("manifest ground_truth expected_fields must be an object")
    return expected_route, expected_fields, ground_truth


def load_cord_samples(config: BenchmarkConfig) -> Iterator[BenchmarkSample]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise E2EError(
            "Hugging Face datasets is required for CORD-v2. "
            "Install with: uv sync --frozen --no-default-groups --group research"
        ) from exc

    dataset = load_dataset(CORD_DATASET_ID, split=config.split, streaming=True)
    if config.shuffle and hasattr(dataset, "shuffle"):
        dataset = dataset.shuffle(seed=config.seed, buffer_size=max(config.limit * 10, 100))

    seen = 0
    emitted = 0
    for row in dataset:
        if seen < config.offset:
            seen += 1
            continue
        sample_id = str(row.get("id") or row.get("image_id") or f"{config.split}-{config.offset + emitted}")
        normalized_truth = extract_cord_ground_truth(row.get("ground_truth"))
        yield BenchmarkSample(
            sample_id=sample_id,
            dataset="cord-v2",
            split=config.split,
            image=row.get("image"),
            ground_truth=normalized_truth["gt_parse"],
            expected_fields=normalized_truth["expected_fields"],
            expected_route=normalized_truth["expected_route"],
            metadata={"source_dataset": CORD_DATASET_ID},
            expected_ocr_text=normalized_truth["expected_ocr_text"],
        )
        emitted += 1
        if emitted >= config.limit:
            break


def load_manifest_samples(config: BenchmarkConfig) -> Iterator[BenchmarkSample]:
    if config.manifest is None:
        raise E2EError("--manifest is required for manifest benchmark mode")
    if not config.manifest.exists():
        raise E2EError(f"manifest not found: {config.manifest}")

    rows = [
        json.loads(line)
        for line in config.manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if config.shuffle:
        random.Random(config.seed).shuffle(rows)

    for idx, row in enumerate(rows[config.offset : config.offset + config.limit]):
        expected_route, expected_fields, ground_truth = manifest_ground_truth(row.get("ground_truth", row))
        image_path = row.get("image_path")
        if not image_path:
            raise E2EError(f"manifest row {idx} missing image_path")
        sample_id = str(row.get("sample_id") or row.get("id") or Path(image_path).stem)
        yield BenchmarkSample(
            sample_id=sample_id,
            dataset=str(row.get("dataset") or "manifest"),
            split=str(row.get("split") or config.split),
            image=Path(image_path),
            ground_truth=ground_truth,
            expected_fields=expected_fields,
            expected_route=expected_route,
            metadata={key: value for key, value in row.items() if key not in {"image_path", "ground_truth"}},
            expected_ocr_text=str(
                ground_truth.get("expected_ocr_text")
                or ground_truth.get("ocr_text")
                or row.get("expected_ocr_text")
                or ""
            ),
            expected_validation_verdict=str(
                ground_truth.get("expected_validation_verdict")
                or ground_truth.get("validation_verdict")
                or row.get("expected_validation_verdict")
                or ""
            ),
        )


def load_samples(config: BenchmarkConfig) -> Iterator[BenchmarkSample]:
    if config.dataset == "cord-v2":
        yield from load_cord_samples(config)
        return
    yield from load_manifest_samples(config)


def write_sample_image(sample: BenchmarkSample, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = sample.image

    if isinstance(image, Path):
        if not image.exists():
            raise E2EError(f"sample image not found: {image}")
        suffix = image.suffix or ".png"
        output_path = destination.with_suffix(suffix)
        shutil.copyfile(image, output_path)
        return output_path

    if isinstance(image, str):
        return write_sample_image(
            BenchmarkSample(
                sample_id=sample.sample_id,
                dataset=sample.dataset,
                split=sample.split,
                image=Path(image),
                ground_truth=sample.ground_truth,
                expected_fields=sample.expected_fields,
                expected_route=sample.expected_route,
                metadata=sample.metadata,
            ),
            destination,
        )

    if isinstance(image, dict):
        path = image.get("path")
        if path:
            return write_sample_image(
                BenchmarkSample(
                    sample_id=sample.sample_id,
                    dataset=sample.dataset,
                    split=sample.split,
                    image=Path(path),
                    ground_truth=sample.ground_truth,
                    expected_fields=sample.expected_fields,
                    expected_route=sample.expected_route,
                    metadata=sample.metadata,
                ),
                destination,
            )
        raw_bytes = image.get("bytes")
        if isinstance(raw_bytes, bytes):
            output_path = destination.with_suffix(".png")
            output_path.write_bytes(raw_bytes)
            return output_path

    if hasattr(image, "save"):
        output_path = destination.with_suffix(".png")
        image.save(output_path)
        return output_path

    raise E2EError(f"unsupported image value for sample {sample.sample_id}: {type(image).__name__}")


def build_runner_config(config: BenchmarkConfig, sample_path: Path, sample: BenchmarkSample, artifact_dir: Path) -> RunnerConfig:
    return RunnerConfig(
        api_url=config.api_url,
        sample_path=sample_path,
        artifact_dir=artifact_dir,
        timeout_seconds=config.timeout_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
        ingestion_api_key=config.ingestion_api_key,
        tenant_id=config.tenant_id,
        actor_id=config.actor_id,
        idempotency_key=f"benchmark:{sample.dataset}:{sample.split}:{sample.sample_id}:{uuid.uuid4()}",
        strict_required_fields=False,
        require_observability=False,
        prometheus_url="",
        collect_logs=False,
        log_tail=0,
    )


def extract_fields_from_result(result_response: dict[str, Any]) -> dict[str, Any]:
    result = result_response.get("result")
    if not isinstance(result, dict):
        return {}
    extraction = result.get("extraction")
    if not isinstance(extraction, dict):
        return {}
    fields = extraction.get("fields")
    return fields if isinstance(fields, dict) else {}


def compare_expected_fields(expected: dict[str, Any], predicted: dict[str, Any]) -> dict[str, Any]:
    comparable = {
        key: value
        for key, value in expected.items()
        if key != "line_item_count" and value not in {"", None}
    }
    matched: dict[str, bool] = {}
    for key, expected_value in comparable.items():
        matched[key] = normalize_field(key, predicted.get(key)) == normalize_field(key, expected_value)

    expected_count = len(comparable)
    predicted_present = sum(1 for key in comparable if normalize_field(key, predicted.get(key)))
    correct = sum(1 for value in matched.values() if value)
    precision = correct / predicted_present if predicted_present else 0.0
    recall = correct / expected_count if expected_count else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0

    line_item_expected = expected.get("line_item_count")
    line_item_predicted = predicted.get("line_item_count") or predicted.get("line_items")
    line_item_match: bool | None = None
    if isinstance(line_item_expected, int):
        if isinstance(line_item_predicted, list):
            line_item_match = len(line_item_predicted) == line_item_expected
        elif line_item_predicted is not None:
            try:
                line_item_match = int(line_item_predicted) == line_item_expected
            except (TypeError, ValueError):
                line_item_match = False

    return {
        "expected_field_count": expected_count,
        "predicted_present_count": predicted_present,
        "correct_field_count": correct,
        "field_precision": round(precision, 6),
        "field_recall": round(recall, 6),
        "field_f1": round(f1, 6),
        "field_exact_match": round(recall, 6),
        "matched_fields": matched,
        "line_item_expected_count": line_item_expected,
        "line_item_match": line_item_match,
    }


def build_sample_metrics(
    *,
    sample: BenchmarkSample,
    result_response: dict[str, Any] | None,
    validation_errors: list[str],
    runtime_status: str,
    latency_seconds: float | None = None,
) -> dict[str, Any]:
    predicted_fields = extract_fields_from_result(result_response or {})
    comparison = compare_expected_fields(sample.expected_fields, predicted_fields)
    result = (result_response or {}).get("result") if isinstance(result_response, dict) else {}
    result = result if isinstance(result, dict) else {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    extraction = result.get("extraction") if isinstance(result.get("extraction"), dict) else {}
    ocr = result.get("ocr") if isinstance(result.get("ocr"), dict) else {}
    classification = result.get("classification") if isinstance(result.get("classification"), dict) else {}

    route = str(extraction.get("route") or validation.get("route") or classification.get("route") or "unknown")
    workflow_status = str(result.get("status") or runtime_status)
    predicted_ocr_text = str(ocr.get("full_text") or "")
    validation_verdict = str(validation.get("verdict") or "unknown")
    expected_validation_verdict = sample.expected_validation_verdict.strip()
    requires_human_review = bool(validation.get("requires_human_review"))
    expected_requires_human_review = safe_float(comparison.get("field_f1")) < 1.0
    validation_correct = (
        validation_verdict == expected_validation_verdict
        if expected_validation_verdict
        else requires_human_review == expected_requires_human_review
    )
    return {
        "sample_id": sample.sample_id,
        "dataset": sample.dataset,
        "split": sample.split,
        "runtime_status": runtime_status,
        "workflow_final_status": workflow_status,
        "expected_route": sample.expected_route,
        "predicted_route": route,
        "route_match": route == sample.expected_route,
        "ocr_confidence": safe_float(ocr.get("mean_confidence")),
        "ocr_reference_available": bool(sample.expected_ocr_text.strip()),
        "ocr_cer": error_rate(sample.expected_ocr_text, predicted_ocr_text, words=False),
        "ocr_wer": error_rate(sample.expected_ocr_text, predicted_ocr_text, words=True),
        "extraction_confidence": safe_float(extraction.get("confidence")),
        "requires_human_review": requires_human_review,
        "validation_verdict": validation_verdict,
        "expected_validation_verdict": expected_validation_verdict or None,
        "expected_requires_human_review": expected_requires_human_review,
        "validation_expectation_source": "explicit_verdict" if expected_validation_verdict else "field_correctness",
        "validation_correct": validation_correct,
        "latency_seconds": round(latency_seconds, 6) if latency_seconds is not None else None,
        "pipeline_contract_passed": not validation_errors,
        "pipeline_contract_errors": validation_errors,
        **comparison,
    }


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile_value
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def aggregate_metrics(
    sample_metrics: list[dict[str, Any]],
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    total = len(sample_metrics)
    completed = sum(1 for item in sample_metrics if item.get("runtime_status") == COMPLETED_STATUS)
    contract_passed = sum(1 for item in sample_metrics if item.get("pipeline_contract_passed"))
    route_matches = sum(1 for item in sample_metrics if item.get("route_match"))
    ocr_cer_values = [safe_float(item["ocr_cer"]) for item in sample_metrics if item.get("ocr_cer") is not None]
    ocr_wer_values = [safe_float(item["ocr_wer"]) for item in sample_metrics if item.get("ocr_wer") is not None]
    validation_values = [
        bool(item["validation_correct"])
        for item in sample_metrics
        if item.get("validation_correct") is not None
    ]
    latency_values = [
        safe_float(item["latency_seconds"])
        for item in sample_metrics
        if item.get("latency_seconds") is not None
    ]
    elapsed = max(0.0, elapsed_seconds or 0.0)
    return {
        "sample_count": total,
        "completed_count": completed,
        "completion_rate": round(completed / total, 6) if total else 0.0,
        "pipeline_contract_pass_count": contract_passed,
        "pipeline_contract_pass_rate": round(contract_passed / total, 6) if total else 0.0,
        "route_accuracy": round(route_matches / total, 6) if total else 0.0,
        "field_precision_mean": round(mean(safe_float(item.get("field_precision")) for item in sample_metrics), 6),
        "field_recall_mean": round(mean(safe_float(item.get("field_recall")) for item in sample_metrics), 6),
        "field_f1_mean": round(mean(safe_float(item.get("field_f1")) for item in sample_metrics), 6),
        "field_exact_match_mean": round(mean(safe_float(item.get("field_exact_match")) for item in sample_metrics), 6),
        "ocr_confidence_mean": round(mean(safe_float(item.get("ocr_confidence")) for item in sample_metrics), 6),
        "ocr_reference_count": len(ocr_cer_values),
        "ocr_cer_mean": round(mean(ocr_cer_values), 6) if ocr_cer_values else None,
        "ocr_wer_mean": round(mean(ocr_wer_values), 6) if ocr_wer_values else None,
        "extraction_confidence_mean": round(mean(safe_float(item.get("extraction_confidence")) for item in sample_metrics), 6),
        "validation_labeled_count": len(validation_values),
        "validation_accuracy": (
            round(sum(validation_values) / len(validation_values), 6) if validation_values else None
        ),
        "benchmark_elapsed_seconds": round(elapsed, 6),
        "throughput_documents_per_minute": round(completed * 60 / elapsed, 6) if elapsed else None,
        "latency_sample_count": len(latency_values),
        "latency_p50_seconds": (
            round(value, 6) if (value := percentile(latency_values, 0.50)) is not None else None
        ),
        "latency_p95_seconds": (
            round(value, 6) if (value := percentile(latency_values, 0.95)) is not None else None
        ),
        "human_review_rate": round(
            sum(1 for item in sample_metrics if item.get("requires_human_review")) / total,
            6,
        )
        if total
        else 0.0,
    }


def evaluate_quality_gate(config: BenchmarkConfig, aggregate: dict[str, Any], error_count: int) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_min(name: str, metric_key: str, threshold: float | None) -> None:
        if threshold is None:
            return
        raw_actual = aggregate.get(metric_key)
        actual = safe_float(raw_actual) if raw_actual is not None else None
        checks.append(
            {
                "name": name,
                "metric": metric_key,
                "operator": ">=",
                "threshold": threshold,
                "actual": actual,
                "passed": actual is not None and actual >= threshold,
            }
        )

    def add_max(name: str, metric_key: str, threshold: float | None) -> None:
        if threshold is None:
            return
        raw_actual = aggregate.get(metric_key)
        actual = safe_float(raw_actual) if raw_actual is not None else None
        checks.append(
            {
                "name": name,
                "metric": metric_key,
                "operator": "<=",
                "threshold": threshold,
                "actual": actual,
                "passed": actual is not None and actual <= threshold,
            }
        )

    add_min("completion_rate", "completion_rate", config.min_completion_rate)
    add_min("sample_count", "sample_count", float(config.min_sample_count) if config.min_sample_count else None)
    add_min("pipeline_contract_pass_rate", "pipeline_contract_pass_rate", config.min_contract_pass_rate)
    add_min("route_accuracy", "route_accuracy", config.min_route_accuracy)
    add_min("field_f1_mean", "field_f1_mean", config.min_field_f1)
    add_min(
        "validation_accuracy",
        "validation_accuracy",
        config.min_validation_accuracy,
    )
    add_min(
        "throughput_documents_per_minute",
        "throughput_documents_per_minute",
        config.min_throughput_documents_per_minute,
    )
    add_max("human_review_rate", "human_review_rate", config.max_human_review_rate)
    add_max("ocr_cer_mean", "ocr_cer_mean", config.max_ocr_cer)
    add_max("ocr_wer_mean", "ocr_wer_mean", config.max_ocr_wer)
    add_max(
        "latency_p95_seconds",
        "latency_p95_seconds",
        config.max_p95_latency_seconds,
    )

    failed_checks = [check for check in checks if not check["passed"]]
    passed = error_count == 0 and not failed_checks
    return {
        "enabled": bool(checks),
        "passed": passed,
        "error_count": error_count,
        "checks": checks,
        "failed_checks": failed_checks,
    }


def track_aggregate_evaluation(config: BenchmarkConfig, aggregate: dict[str, Any]) -> dict[str, Any] | None:
    if not config.track_evaluation or config.dry_run:
        return None
    payload = {
        "job_id": f"benchmark-{config.dataset}-{config.split}-{int(time.time())}",
        "status": "benchmark_completed",
        "ocr_confidence": safe_float(aggregate.get("ocr_confidence_mean")),
        "extraction_confidence": safe_float(aggregate.get("extraction_confidence_mean")),
        "used_vlm_fallback": False,
        "requires_human_review": safe_float(aggregate.get("human_review_rate")) > 0,
        "route": config.dataset,
        "validation_verdict": "benchmark",
        "field_count": int(aggregate.get("sample_count") or 0),
        "populated_field_count": int(aggregate.get("completed_count") or 0),
        "custom_metrics": {
            key: safe_float(value)
            for key, value in aggregate.items()
            if isinstance(value, (int, float)) and key not in {"sample_count", "completed_count"}
        },
        "parameters": {
            "benchmark_dataset": config.dataset,
            "benchmark_split": config.split,
            "benchmark_limit": config.limit,
            "benchmark_offset": config.offset,
            "benchmark_concurrency": config.concurrency,
        },
        "tags": {
            "benchmark_suite": "dataset_benchmark",
            "dataset": config.dataset,
            "split": config.split,
        },
        "dataset_version": f"{config.dataset}:{config.split}",
        "pipeline_version": config.pipeline_version,
        "idempotency_key": f"benchmark:{config.dataset}:{config.split}:{uuid.uuid4()}",
    }
    return http_json("POST", f"{config.evaluation_url}/track-run", body=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, timeout=60)


def run_sample(config: BenchmarkConfig, sample: BenchmarkSample, index: int) -> dict[str, Any]:
    started = time.perf_counter()
    sample_dir = config.artifact_dir / "samples" / f"{index:04d}-{sample.sample_id}"
    sample_dir.mkdir(parents=True, exist_ok=True)
    image_path = write_sample_image(sample, sample_dir / "document")
    write_json(
        sample_dir / "expected.json",
        {
            "sample_id": sample.sample_id,
            "dataset": sample.dataset,
            "split": sample.split,
            "expected_route": sample.expected_route,
            "expected_fields": sample.expected_fields,
            "ground_truth": sample.ground_truth,
            "metadata": sample.metadata,
            "image_path": str(image_path),
        },
    )

    if config.dry_run:
        metrics = build_sample_metrics(
            sample=sample,
            result_response=None,
            validation_errors=["dry_run"],
            runtime_status="DRY_RUN",
            latency_seconds=time.perf_counter() - started,
        )
        metrics.update(
            {
                "ocr_cer": None,
                "ocr_wer": None,
                "validation_correct": None,
                "latency_seconds": None,
            }
        )
        write_json(sample_dir / "metrics.json", metrics)
        return metrics

    runner_config = build_runner_config(config, image_path, sample, sample_dir)
    write_json(sample_dir / "runner_config.json", {"api_url": config.api_url, "tenant_id": config.tenant_id, "actor_id": config.actor_id})
    submission = submit_document(runner_config)
    write_json(sample_dir / "submission.json", submission)
    job_id = str(submission.get("job_id") or "")
    if not job_id:
        raise E2EError(f"sample {sample.sample_id}: submission did not return job_id")

    print(f"[{index}] sample_id={sample.sample_id} job_id={job_id}", flush=True)
    final_status, history = poll_until_complete(runner_config, job_id)
    write_json(sample_dir / "status_history.json", history)
    write_json(sample_dir / "final_status.json", final_status)

    runtime_status = str(final_status.get("workflow_status") or final_status.get("status") or "UNKNOWN")
    result_response: dict[str, Any] | None = None
    validation_errors: list[str] = []
    if runtime_status == COMPLETED_STATUS:
        result_response = get_result(runner_config, job_id)
        write_json(sample_dir / "result.json", result_response)
        _, validation_errors = validate_pipeline_result(result_response, strict_required_fields=False)
    else:
        validation_errors = [f"workflow_not_completed:{runtime_status}"]

    metrics = build_sample_metrics(
        sample=sample,
        result_response=result_response,
        validation_errors=validation_errors,
        runtime_status=runtime_status,
        latency_seconds=time.perf_counter() - started,
    )
    metrics["job_id"] = job_id
    metrics["workflow_id"] = submission.get("workflow_id") or final_status.get("workflow_id")
    write_json(sample_dir / "metrics.json", metrics)
    return metrics


def run(config: BenchmarkConfig) -> dict[str, Any]:
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    write_json(config.artifact_dir / "config.json", redacted_config(config))

    sample_metrics: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    samples = list(enumerate(load_samples(config), start=1))
    benchmark_started = time.perf_counter()

    def execute(index: int, sample: BenchmarkSample) -> tuple[int, BenchmarkSample, dict[str, Any] | None, Exception | None]:
        try:
            return index, sample, run_sample(config, sample, index), None
        except Exception as exc:  # noqa: BLE001
            return index, sample, None, exc

    results: list[tuple[int, BenchmarkSample, dict[str, Any] | None, Exception | None]]
    if config.concurrency == 1:
        results = [execute(index, sample) for index, sample in samples]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as executor:
            futures = [executor.submit(execute, index, sample) for index, sample in samples]
            results = [future.result() for future in concurrent.futures.as_completed(futures)]

    for index, sample, metrics, run_error in sorted(results, key=lambda item: item[0]):
        if run_error is None and metrics is not None:
            sample_metrics.append(metrics)
            continue
        errors.append({"sample_id": sample.sample_id, "error": str(run_error)})
        write_json(config.artifact_dir / "errors.json", errors)
        print(f"[{index}] sample_id={sample.sample_id} failed: {run_error}", file=sys.stderr, flush=True)

    elapsed_seconds = time.perf_counter() - benchmark_started
    aggregate = aggregate_metrics(sample_metrics, elapsed_seconds)
    quality_gate = evaluate_quality_gate(config, aggregate, len(errors))
    summary = {
        "dataset": config.dataset,
        "split": config.split,
        "artifact_dir": str(config.artifact_dir),
        "metrics": aggregate,
        "error_count": len(errors),
        "errors": errors,
        "quality_gate": quality_gate,
    }
    write_json(config.artifact_dir / "sample_metrics.json", sample_metrics)
    write_json(config.artifact_dir / "quality_gate.json", quality_gate)
    write_json(config.artifact_dir / "summary.json", summary)

    try:
        evaluation_receipt = track_aggregate_evaluation(config, aggregate)
    except Exception as exc:  # noqa: BLE001
        summary["evaluation_error"] = str(exc)
        write_json(config.artifact_dir / "evaluation_error.json", {"error": str(exc)})
        write_json(config.artifact_dir / "summary.json", summary)
    else:
        if evaluation_receipt is not None:
            write_json(config.artifact_dir / "evaluation_receipt.json", evaluation_receipt)
            summary["evaluation_receipt"] = evaluation_receipt
            write_json(config.artifact_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    try:
        summary = run(build_config(parse_args(argv or sys.argv[1:])))
    except E2EError as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1

    print("Benchmark summary:")
    print(json.dumps(summary, indent=2, sort_keys=True))
    quality_gate = summary.get("quality_gate") if isinstance(summary.get("quality_gate"), dict) else {}
    return 0 if quality_gate.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
