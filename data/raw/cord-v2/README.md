---
license: cc-by-4.0
---

# CORD-v2 Dataset

CORD-v2 is the first recommended public benchmark dataset for the receipt-extraction lane of this IDP project.

Source: `naver-clova-ix/cord-v2` on Hugging Face.

## Why It Fits This Project

CORD-v2 provides:
- Receipt images.
- Structured JSON ground truth.
- Train, validation, and test splits.
- Receipt totals, subtotal, tax, cash/change, and line-item structures.

It is useful for:
- OCR robustness checks.
- Receipt route classification.
- Structured field extraction evaluation.
- Human-review gating behavior.
- Regression testing after OCR, classifier, extraction, validation, or prompt/rule changes.

It is not enough by itself for production readiness because it is receipt-focused. Use it alongside invoices, purchase orders, contracts, statements, and private gold datasets.

## Local Dependency Setup

The CORD-v2 runner uses Hugging Face `datasets` and image decoding support:

```bash
uv sync --frozen --no-default-groups --group research
```

The `research` dependency group includes:
- `datasets`
- `huggingface-hub`
- `pillow`
- `polars`

## Running CORD-v2 Benchmarks

Start the IDP stack first:

```bash
docker compose up -d
```

Run a small validation benchmark:

```bash
INGESTION_API_KEY=dev-ingestion-key TENANT_ID=default \
  .venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 5 \
  --api-url http://localhost:8000 \
  --evaluation-url http://localhost:8018
```

Run a dry-run to verify dataset loading without submitting documents:

```bash
.venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split validation \
  --limit 3 \
  --dry-run \
  --no-track-evaluation
```

Run a shuffled sample:

```bash
.venv/bin/python scripts/run_dataset_benchmark.py \
  --dataset cord-v2 \
  --split test \
  --limit 20 \
  --shuffle \
  --seed 42
```

## Field Mapping

CORD-v2 ground truth is stored in `ground_truth.gt_parse`. The benchmark runner maps it into this internal receipt benchmark schema:

| CORD-v2 path | Benchmark field |
| --- | --- |
| `gt_parse.total.total_price` | `total_amount` |
| `gt_parse.sub_total.tax_price` | `tax_amount` |
| `gt_parse.sub_total.subtotal_price` | `subtotal_amount` |
| `gt_parse.total.cashprice` | `cash_amount` |
| `gt_parse.total.changeprice` | `change_amount` |
| `gt_parse.menu` length | `line_item_count` |

The expected route is:

```text
receipt
```

## Metric Behavior

The runner compares normalized expected fields against the extraction output returned by the full Temporal workflow.

Amount normalization handles common CORD formats:
- `Rp 51.000`
- `52,416`
- `$1,234.50`
- `43.636`

Aggregate metrics include:
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

## Output

Benchmark artifacts are written to:

```text
artifacts/benchmarks/cord-v2-<split>-<run-id>/
```

Important files:
- `summary.json`
- `sample_metrics.json`
- `evaluation_receipt.json`
- `samples/<index>-<sample_id>/expected.json`
- `samples/<index>-<sample_id>/result.json`
- `samples/<index>-<sample_id>/metrics.json`

## How To Interpret Current Results

If the OCR service is forced into fallback mode or PaddleOCR fails, CORD-v2 samples may complete through the HITL branch with low extraction scores. That is still useful:
- It proves the pipeline does not crash.
- It proves validation routes low-confidence extraction to review.
- It gives a baseline for OCR/runtime improvements.

For production-quality receipt extraction, the target trend should be:
- OCR fallback rate decreases.
- `route_accuracy` approaches 1.0 for receipts.
- `field_f1_mean` improves over time.
- `human_review_rate` decreases only when field accuracy remains high.

Do not lower validation thresholds just to make CORD-v2 look better. The benchmark should expose weaknesses, not hide them.
