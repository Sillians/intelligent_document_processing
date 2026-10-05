# Representative Quality Gate

## Evidence Boundary (2026-10-03)
The measurement framework is implemented; no permissioned, independently labeled
representative real-document corpus has been supplied in this workspace. Existing
bootstrap documents are synthetic. Their results can test pipeline mechanics but
cannot approve production accuracy. Do not relabel generated examples as real.

Obtain a held-out corpus reflecting actual document traffic: vendor/template
diversity, languages, scan defects, native/scanned/mixed PDFs, TIFFs, page counts,
and critical fields. Keep families and templates disjoint from tuning data where
possible. Record usage permission, retention and independent label review; never
place sensitive documents or extracted outputs in public artifacts.

## Manifest
Use runner-local JSONL with actual absolute paths and computed SHA-256. Each row:

```json
{
  "sample_id": "held-out-invoice-001",
  "image_path": "/secure/gold/invoice-001.pdf",
  "sha256": "REPLACE_WITH_ACTUAL_SHA256",
  "document_family": "invoice",
  "expected_page_count": 2,
  "synthetic": false,
  "label_review_status": "approved",
  "split": "held-out",
  "provenance": {
    "source": "permissioned corpus identifier",
    "usage_permission": "approved permission record identifier",
    "label_reviewer": "independent reviewer identifier"
  },
  "ground_truth": {
    "expected_route": "invoice",
    "expected_fields": {"invoice_number": "ACTUAL_LABEL", "total_amount": "ACTUAL_LABEL"},
    "expected_ocr_text": "ACTUAL_TRANSCRIPTION_IN_PAGE_ORDER",
    "expected_validation_verdict": "needs_review"
  }
}
```

This example is a schema illustration, not a valid dataset or a permission grant.
Label validation verdicts independently against the agreed rule policy, not by
assuming extraction errors imply a particular verdict. Annotation scope must
state which fields are targets. Current field diagnostics score annotated
positive scalar fields, not unannotated fields or full table structure. Include
separate negative/hallucination audits before accepting those capabilities.

## Measure
From the repository root, with the real manifest and staging credentials:

```bash
.venv/bin/python -m scripts.validate_benchmark_manifest \
  --manifest "$STAGING_BENCHMARK_MANIFEST" \
  --policy infra/staging/release_acceptance.json --limit 60 \
  --output artifacts/quality/manifest-preflight.json
.venv/bin/python -m scripts.run_dataset_benchmark \
  --manifest "$STAGING_BENCHMARK_MANIFEST" --limit 60 \
  --api-url "$STAGING_PUBLIC_BASE_URL" --ingestion-api-key "$STAGING_SMOKE_API_KEY" \
  --thresholds-file infra/staging/release_acceptance.json \
  --artifact-dir artifacts/quality/representative
```

Check `--help` for tenant, concurrency and timeout controls. Benchmarks submit
through intake and measure the configured workflow path. HITL latency includes
review wait; reports must state whether the target is automated or reviewed
delivery. Do not conflate pipeline latency with CPU stage duration.

Reports include CER/WER, extraction precision/recall/F1, explicit-label-only
validation accuracy, throughput, latency/p95 and review rate. Additional
diagnostics: `by_document_family`, `by_field` TP/FP/FN, and
`document_confidence_calibration` reliability bins/ECE against annotated document
exact match. ECE is diagnostic, not a fitted per-field probability calibration;
use a separate calibration split before deploying confidence thresholds.

The field F1 floor applies to every observed family as well as the overall mean,
so a common easy family cannot hide a weak family. Missing validation labels
produce null accuracy, not invented ground truth. Policy adds
`min_validation_labeled_count` and `min_latency_sample_count`; undersampled p95
cannot pass a configured coverage requirement. CER/WER likewise require reference
coverage. Prespecified corpus membership and failures must be retained in evidence.

For policy status `approved`, direct benchmark execution performs manifest
preflight before any submission, requires explicit `synthetic:false` and reviewed
labels, and currently supports only unshuffled, offset-zero manifest selections.
Public-dataset adapters and dry runs cannot substitute for that exact reviewed
selection. Human review of permission declarations remains mandatory.

## Acceptance And Improvement
The current staging policy remains `bootstrap`; its thresholds are not an
approved production policy. Establish a real baseline, inspect failures by
family/field/page, link corrections to source evidence, and review thresholds
against business error costs. Track engine/model versions, exact image SHA,
manifest digest and policy revision. Re-run on the fixed holdout before promotion.
Only then compare stage/transport/queue measurements to justify service-boundary
changes. Representative accuracy, fitted calibration, evidence-backed extraction
fields and production acceptance remain pending real corpus work.
