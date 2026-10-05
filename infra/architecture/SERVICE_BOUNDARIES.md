# Service Boundary Decision Record

Status: provisional, pending a representative live run of schema 2.0.

## Evidence Required

Run `scripts/run_dataset_benchmark.py` with `--manifest` containing at least three labeled document families, five samples per family, five multi-page files, and fifteen OCR text references. The versioned `infra/staging/release_acceptance.json` also requires page-count accuracy of 1.0 plus the existing OCR CER/WER, extraction F1, route, validation, throughput, and p95 latency gates. That policy remains `bootstrap`; do not set `approved` until real staging evidence passes and is reviewed. A single invoice or CORD receipts alone are not representative.

Run `python3 scripts/assess_service_boundaries.py --summary artifacts/benchmarks/<run>/summary.json --output artifacts/benchmarks/<run>/boundary_report.json`. The report withholds consolidation candidates unless the quality gate passed with representative coverage. Stage p95 values are compute/service durations, not network or queue overhead. Measure HTTP, object store, and Temporal spans separately before approving a merge.

## Current Boundary Decision

| Boundary | Provisional decision | Reason |
| --- | --- | --- |
| Ingestion | Keep | Tenant auth, raw persistence, recoverable dispatch outbox |
| Temporal orchestrator | Keep | Durable retries and multi-day HITL state |
| OCR | Keep | Distinct CPU/GPU scale, model lifecycle, and failure mode |
| Human review | Keep | Reviewer decision durability and separate credentials |
| Delivery | Keep | External side effects and replay protection |
| Preprocess | Keep pending profiling | PDF rasterization and image processing are CPU/memory intensive; profile separately from OCR |
| Layout | Keep pending profiling | Heavy optional model; page-aware accuracy must justify separate deployment |
| Extraction | Keep pending profiling | LLM inference may require distinct infrastructure; deterministic mode may not |
| Classifier and validation | Candidate to combine only after evidence | If each has <=10 ms service p95 on a passing representative run, measure transport/queue cost and failure isolation before co-location |

Do not remove a service merely because it has a low compute duration. Compare end-to-end p95, throughput, deployment size, crash isolation, ownership, and tenant/security boundary costs before changing Compose or Temporal activity topology. Re-run the same labeled suite and rollback drill after any merge.
