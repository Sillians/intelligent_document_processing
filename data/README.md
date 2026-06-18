# Dataset Strategy

This project uses datasets in layers. Do not treat one public dataset as the whole IDP validation strategy.

## Dataset Layers

1. `Smoke samples`
- Purpose: fast local sanity checks.
- Example: `samples/documents/sample_invoice_001.png`.
- Runner: `scripts/run_e2e.sh`.
- Expected use: every service wiring change and local stack check.

2. `Public benchmark datasets`
- Purpose: repeatable metrics for known document-understanding tasks.
- Examples:
  - `cord-v2`: receipt extraction benchmark.
  - `ICDAR2019-SROIE`: receipt OCR and key information extraction.
  - `DocLayNet-v1.1`: layout segmentation benchmark.
- Runner: `scripts/run_dataset_benchmark.py`.

3. `Private gold datasets`
- Purpose: production-readiness gates for your real business documents.
- Format: JSONL manifest with image path and expected fields.
- Source: redacted/human-reviewed production samples.
- Expected use: release gates, model/prompt changes, extraction rule changes.

4. `Synthetic and augmented datasets`
- Purpose: robustness checks.
- Examples: skew, blur, low contrast, JPEG artifacts, shadows, rotated scans, cropped photos.
- Expected use: OCR and preprocessing regression tests.

5. `Production feedback datasets`
- Purpose: continuous improvement.
- Source: human-review corrections, analyst feedback, delivery rejections.
- Requirement: privacy review, redaction, tenant isolation, and data-retention policy.

## Public Dataset Runner

Use the benchmark runner for public and local gold datasets:

```bash
python3 scripts/run_dataset_benchmark.py --help
```

CORD-v2 example:

```bash
uv sync --frozen --no-default-groups --group research

INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default \
  .venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 5 \
  --api-url http://localhost:8081 \
  --evaluation-url http://localhost:8018
```

Dry-run CORD-v2 loading without submitting to the IDP stack:

```bash
.venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 3 \
  --dry-run \
  --no-track-evaluation
```

## Local Gold Manifest Format

Use JSONL for private gold datasets. Each line is one sample.

```json
{"sample_id":"invoice-001","image_path":"/absolute/path/to/invoice-001.png","ground_truth":{"expected_route":"invoice","expected_fields":{"invoice_number":"INV-001","invoice_date":"2026/06/08","total_amount":"3186.00"}}}
```

An example manifest is included at:

```text
data/benchmarks/example_manifest.jsonl
```

Run a local manifest:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default \
  python3 scripts/run_dataset_benchmark.py \
  --manifest data/benchmarks/example_manifest.jsonl \
  --split validation \
  --limit 10
```

Run a milestone gate against the local stack:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default \
  python3 scripts/run_dataset_benchmark.py \
  --manifest data/benchmarks/example_manifest.jsonl \
  --split validation \
  --limit 1 \
  --api-url http://localhost:8081 \
  --evaluation-url http://localhost:8018 \
  --no-track-evaluation \
  --min-completion-rate 1 \
  --min-contract-pass-rate 1 \
  --min-route-accuracy 0 \
  --min-field-f1 0 \
  --max-human-review-rate 1
```

For a real representative gold set, raise the quality thresholds over time. Start with
completion and contract pass rates at `1`, then ratchet field F1, route accuracy, and
human-review rate as extraction quality improves.

## Output Artifacts

Benchmark outputs are written to:

```text
artifacts/benchmarks/<dataset>-<split>-<run-id>/
```

Important files:
- `config.json`: redacted runner configuration.
- `samples/<index>-<sample_id>/expected.json`: expected fields and ground truth.
- `samples/<index>-<sample_id>/submission.json`: ingestion response.
- `samples/<index>-<sample_id>/status_history.json`: workflow polling history.
- `samples/<index>-<sample_id>/result.json`: final workflow result when completed.
- `samples/<index>-<sample_id>/metrics.json`: per-sample benchmark metrics.
- `sample_metrics.json`: all sample metrics.
- `summary.json`: aggregate benchmark metrics.
- `quality_gate.json`: pass/fail details for configured benchmark thresholds.
- `evaluation_receipt.json`: evaluation-service response when tracking is enabled.

`artifacts/benchmarks/` is git-ignored because benchmark output can be large and should be promoted deliberately.

## Metrics

The benchmark runner currently computes:
- `completion_rate`
- `pipeline_contract_pass_rate`
- `route_accuracy`
- `field_precision_mean`
- `field_recall_mean`
- `field_f1_mean`
- `field_exact_match_mean`
- `ocr_confidence_mean`
- `extraction_confidence_mean`
- `human_review_rate`

These aggregate metrics are sent to `evaluation-service` by default unless `--no-track-evaluation` is set.

Quality gate flags:
- `--min-completion-rate`
- `--min-contract-pass-rate`
- `--min-route-accuracy`
- `--min-field-f1`
- `--max-human-review-rate`

## Best Practice

Use this promotion path:

```text
smoke sample passes
  -> small public benchmark passes
  -> private gold validation passes
  -> production canary
```

Do not optimize only for CORD-v2. It is useful for receipt extraction, but production IDP quality must be measured on your target document mix.
