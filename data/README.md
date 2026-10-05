# Dataset Strategy

## Representative Gate Update (2026-10-03)
See `infra/quality/README.md` for the real-document manifest contract and runner
commands. Approved evidence requires explicit non-synthetic provenance, independent
label review, actual content hashes and reviewed permissions. Missing validation
labels are no longer inferred from field correctness. Per-family/per-field
diagnostics and confidence reliability bins reveal weaknesses hidden by averages.
The current synthetic bootstrap remains functional evidence only; a permissioned
real held-out corpus and reviewed acceptance policy are still required.

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
{"sample_id":"invoice-001","image_path":"/absolute/path/to/invoice-001.png","ground_truth":{"expected_route":"invoice","expected_ocr_text":"Invoice INV-001 ...","expected_validation_verdict":"auto_approved","expected_fields":{"invoice_number":"INV-001","invoice_date":"2026/06/08","total_amount":"3186.00"}}}
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

Use the versioned staging acceptance profile with a representative manifest:

```bash
INGESTION_API_KEY="$STAGING_SMOKE_API_KEY" TENANT_ID=default \
  python3 scripts/run_dataset_benchmark.py \
  --manifest /private/gold/manifest.jsonl \
  --split validation \
  --limit 20 \
  --concurrency 1 \
  --api-url http://127.0.0.1:8081 \
  --thresholds-file infra/staging/release_acceptance.json \
  --no-track-evaluation
```

CORD-v2 may be used for a receipt-only diagnostic, but it cannot pass this
multi-family, multi-page release gate. The committed profile is marked `bootstrap`. Run a representative baseline,
review the evidence, then change it to an approved policy rather than weakening
individual checks after a failed release.

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
- `ocr_cer_mean`
- `ocr_wer_mean`
- `extraction_confidence_mean`
- `validation_accuracy`
- `throughput_documents_per_minute`
- `latency_p50_seconds`
- `latency_p95_seconds`
- `human_review_rate`

These aggregate metrics are sent to `evaluation-service` by default unless `--no-track-evaluation` is set.

CER/WER are recorded only when `expected_ocr_text` is present. CORD-v2 uses its
`valid_line.words` transcription. Validation accuracy compares an explicit
`expected_validation_verdict` when supplied; otherwise it checks whether the
validation gate sends imperfect field extraction to review and auto-approves
exact field matches.

Quality gate flags:
- `--min-sample-count`
- `--min-completion-rate`
- `--min-contract-pass-rate`
- `--min-route-accuracy`
- `--min-field-f1`
- `--min-validation-accuracy`
- `--min-throughput-documents-per-minute`
- `--max-human-review-rate`
- `--max-ocr-cer`
- `--max-ocr-wer`
- `--max-p95-latency-seconds`
- `--thresholds-file`

Use `--concurrency` to record a controlled load profile. Compare runs only when
dataset, split, sample count, concurrency, OCR backend, and host resources match.

## Best Practice

Use this promotion path:

```text
smoke sample passes
  -> small public benchmark passes
  -> private gold validation passes
  -> production canary
```

Do not optimize only for CORD-v2. It is useful for receipt extraction, but production IDP quality must be measured on your target document mix.
# Representative Page-Aware Release Gate (2026-09-30)

The one-invoice example manifest is a smoke sample, **not** release evidence. Build a private JSONL manifest of real or properly licensed labeled documents that includes at least three families (for example invoices, receipts, and contracts), at least five samples in each family, at least five documents with two or more pages, and at least fifteen verbatim OCR references. Use disjoint documents for development and release evaluation. A manifest row should include `document_family` and `expected_page_count` at the top level, plus `ground_truth.expected_route`, `ground_truth.expected_fields`, and `ground_truth.expected_ocr_text` where labeled:

```json
{"sample_id":"redacted-case-01","image_path":"/private/gold/case-01.pdf","document_family":"contract","expected_page_count":3,"ground_truth":{"expected_route":"contract","expected_fields":{"effective_date":"2026-01-01"},"expected_ocr_text":"verbatim text from all three pages"}}
```

The benchmark now records `document_family_counts`, `multi_page_sample_count`, `ocr_reference_count`, `page_count_accuracy`, and per-stage p95 service durations. Missing references are not scored as zero-error OCR; the minimum OCR-reference count blocks a false pass. Page-count mismatch blocks a correct-page claim. Run the existing benchmark with `--manifest /private/gold/manifest.jsonl --thresholds-file infra/staging/release_acceptance.json` and inspect `summary.json`, `sample_metrics.json`, and `quality_gate.json`. The policy remains `bootstrap` until a representative staging run is reviewed. After it passes, generate `boundary_report.json` with `scripts/assess_service_boundaries.py`; do not interpret stage compute time alone as network overhead.

For GitHub Actions `Benchmark Staging`, set the staging environment variable `STAGING_BENCHMARK_MANIFEST` to the absolute path of a readable private JSONL file on the self-hosted runner. Its `image_path` entries must also resolve on that runner. The workflow no longer uses CORD-v2 as release-acceptance evidence; CORD can still be run manually as a receipt-only diagnostic. The workflow uploads `boundary_report.json` alongside its quality evidence.
