from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_dataset_benchmark import (
    BenchmarkConfig,
    aggregate_metrics,
    build_config,
    compare_expected_fields,
    error_rate,
    evaluate_quality_gate,
    extract_cord_ground_truth,
    load_manifest_samples,
    normalize_amount,
    parse_args,
)


class DatasetBenchmarkTests(unittest.TestCase):
    def test_normalize_amount_handles_cord_formats(self) -> None:
        self.assertEqual(normalize_amount("Rp 51.000"), "51000")
        self.assertEqual(normalize_amount("52,416"), "52416")
        self.assertEqual(normalize_amount("$1,234.50"), "1234.5")
        self.assertEqual(normalize_amount("43.636"), "43636")

    def test_extract_cord_ground_truth_maps_receipt_fields(self) -> None:
        raw = {
            "gt_parse": {
                "menu": [
                    {"nm": "Tea", "cnt": "1", "price": "10.000"},
                    {"nm": "Coffee", "cnt": "2", "price": "20.000"},
                ],
                "sub_total": {"subtotal_price": "30.000", "tax_price": "3.000"},
                "total": {"total_price": "33.000", "cashprice": "50.000", "changeprice": "17.000"},
            }
        }

        parsed = extract_cord_ground_truth(json.dumps(raw))

        self.assertEqual(parsed["expected_route"], "receipt")
        self.assertEqual(parsed["expected_fields"]["total_amount"], "33.000")
        self.assertEqual(parsed["expected_fields"]["tax_amount"], "3.000")
        self.assertEqual(parsed["expected_fields"]["line_item_count"], 2)

    def test_compare_expected_fields_scores_normalized_matches(self) -> None:
        comparison = compare_expected_fields(
            {
                "total_amount": "33.000",
                "tax_amount": "3.000",
                "merchant_name": "Acme Cafe",
            },
            {
                "total_amount": "33000",
                "tax_amount": "Rp 3,000",
                "merchant_name": "ACME CAFE",
            },
        )

        self.assertEqual(comparison["correct_field_count"], 3)
        self.assertEqual(comparison["field_f1"], 1.0)
        self.assertEqual(comparison["matched_fields"]["merchant_name"], True)

    def test_aggregate_metrics(self) -> None:
        metrics = aggregate_metrics(
            [
                {
                    "runtime_status": "COMPLETED",
                    "pipeline_contract_passed": True,
                    "route_match": True,
                    "field_precision": 1.0,
                    "field_recall": 0.5,
                    "field_f1": 0.666667,
                    "field_exact_match": 0.5,
                    "ocr_confidence": 0.8,
                    "ocr_cer": 0.1,
                    "ocr_wer": 0.2,
                    "extraction_confidence": 0.7,
                    "validation_correct": True,
                    "latency_seconds": 10.0,
                    "requires_human_review": False,
                },
                {
                    "runtime_status": "FAILED",
                    "pipeline_contract_passed": False,
                    "route_match": False,
                    "field_precision": 0.0,
                    "field_recall": 0.0,
                    "field_f1": 0.0,
                    "field_exact_match": 0.0,
                    "ocr_confidence": 0.2,
                    "ocr_cer": 0.3,
                    "ocr_wer": 0.4,
                    "extraction_confidence": 0.1,
                    "validation_correct": False,
                    "latency_seconds": 20.0,
                    "requires_human_review": True,
                },
            ],
            elapsed_seconds=30,
        )

        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["completion_rate"], 0.5)
        self.assertEqual(metrics["route_accuracy"], 0.5)
        self.assertEqual(metrics["human_review_rate"], 0.5)
        self.assertEqual(metrics["ocr_cer_mean"], 0.2)
        self.assertEqual(metrics["ocr_wer_mean"], 0.3)
        self.assertEqual(metrics["validation_accuracy"], 0.5)
        self.assertEqual(metrics["throughput_documents_per_minute"], 2.0)
        self.assertEqual(metrics["latency_p50_seconds"], 15.0)
        self.assertEqual(metrics["latency_p95_seconds"], 19.5)

    def test_ocr_error_rates_use_character_and_word_units(self) -> None:
        self.assertAlmostEqual(error_rate("total amount", "total amont", words=False) or 0, 1 / 12, places=6)
        self.assertEqual(error_rate("total amount", "total amont", words=True), 0.5)
        self.assertIsNone(error_rate("", "anything", words=False))

    def test_manifest_loader_supports_private_gold_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            image_path = tmp_path / "sample.png"
            image_path.write_bytes(b"not-a-real-image-for-loader-only")
            manifest_path = tmp_path / "manifest.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "sample_id": "sample-1",
                        "image_path": str(image_path),
                        "ground_truth": {
                            "expected_route": "invoice",
                            "expected_fields": {"invoice_number": "INV-1"},
                            "expected_ocr_text": "Invoice INV-1",
                            "expected_validation_verdict": "auto_approved",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = BenchmarkConfig(
                dataset="manifest",
                split="test",
                manifest=manifest_path,
                limit=1,
                offset=0,
                shuffle=False,
                seed=1,
                api_url="http://localhost:8000",
                evaluation_url="http://localhost:8018",
                artifact_dir=tmp_path / "artifacts",
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                ingestion_api_key="secret",
                tenant_id="default",
                actor_id="test",
                pipeline_version="test",
                track_evaluation=False,
                dry_run=True,
                concurrency=1,
                thresholds_file=None,
                min_sample_count=None,
                min_completion_rate=None,
                min_contract_pass_rate=None,
                min_route_accuracy=None,
                min_field_f1=None,
                min_validation_accuracy=None,
                min_throughput_documents_per_minute=None,
                max_human_review_rate=None,
                max_ocr_cer=None,
                max_ocr_wer=None,
                max_p95_latency_seconds=None,
            )

            sample = next(load_manifest_samples(config))

            self.assertEqual(sample.sample_id, "sample-1")
            self.assertEqual(sample.expected_route, "invoice")
            self.assertEqual(sample.expected_fields["invoice_number"], "INV-1")
            self.assertEqual(sample.expected_ocr_text, "Invoice INV-1")
            self.assertEqual(sample.expected_validation_verdict, "auto_approved")

    def test_quality_gate_passes_when_thresholds_are_met(self) -> None:
        config = self._benchmark_config(
            min_completion_rate=0.9,
            min_contract_pass_rate=0.9,
            min_route_accuracy=0.8,
            min_field_f1=0.7,
            max_human_review_rate=0.5,
            max_ocr_cer=0.3,
            max_ocr_wer=0.4,
            min_validation_accuracy=0.8,
            min_throughput_documents_per_minute=1.0,
            max_p95_latency_seconds=30.0,
        )
        aggregate = {
            "completion_rate": 1.0,
            "pipeline_contract_pass_rate": 1.0,
            "route_accuracy": 0.9,
            "field_f1_mean": 0.75,
            "human_review_rate": 0.4,
            "ocr_cer_mean": 0.2,
            "ocr_wer_mean": 0.3,
            "validation_accuracy": 1.0,
            "throughput_documents_per_minute": 2.0,
            "latency_p95_seconds": 20.0,
        }

        gate = evaluate_quality_gate(config, aggregate, error_count=0)

        self.assertTrue(gate["enabled"])
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["failed_checks"], [])

    def test_quality_gate_reports_failed_thresholds(self) -> None:
        config = self._benchmark_config(min_completion_rate=1.0, min_field_f1=0.8, max_human_review_rate=0.25)
        aggregate = {
            "completion_rate": 0.5,
            "pipeline_contract_pass_rate": 1.0,
            "route_accuracy": 1.0,
            "field_f1_mean": 0.6,
            "human_review_rate": 0.5,
        }

        gate = evaluate_quality_gate(config, aggregate, error_count=0)

        self.assertFalse(gate["passed"])
        self.assertEqual(
            [check["metric"] for check in gate["failed_checks"]],
            ["completion_rate", "field_f1_mean", "human_review_rate"],
        )

    def test_quality_gate_fails_when_requested_metric_is_unavailable(self) -> None:
        config = self._benchmark_config(max_ocr_cer=0.3)
        gate = evaluate_quality_gate(config, {"ocr_cer_mean": None}, error_count=0)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["failed_checks"][0]["actual"], None)

    def test_threshold_file_populates_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            threshold_path = Path(tmp) / "thresholds.json"
            threshold_path.write_text(
                json.dumps(
                    {
                        "thresholds": {
                            "min_sample_count": 20,
                            "max_ocr_cer": 0.25,
                            "max_p95_latency_seconds": 90,
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = build_config(
                parse_args(
                    [
                        "--manifest",
                        "data/benchmarks/example_manifest.jsonl",
                        "--thresholds-file",
                        str(threshold_path),
                    ]
                )
            )

        self.assertEqual(config.min_sample_count, 20)
        self.assertEqual(config.max_ocr_cer, 0.25)
        self.assertEqual(config.max_p95_latency_seconds, 90)

    def _benchmark_config(self, **overrides: object) -> BenchmarkConfig:
        defaults: dict[str, object] = {
            "dataset": "manifest",
            "split": "test",
            "manifest": None,
            "limit": 1,
            "offset": 0,
            "shuffle": False,
            "seed": 1,
            "api_url": "http://localhost:8000",
            "evaluation_url": "http://localhost:8018",
            "artifact_dir": Path("artifacts/test"),
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.1,
            "ingestion_api_key": "secret",
            "tenant_id": "default",
            "actor_id": "test",
            "pipeline_version": "test",
            "track_evaluation": False,
            "dry_run": True,
            "concurrency": 1,
            "thresholds_file": None,
            "min_sample_count": None,
            "min_completion_rate": None,
            "min_contract_pass_rate": None,
            "min_route_accuracy": None,
            "min_field_f1": None,
            "min_validation_accuracy": None,
            "min_throughput_documents_per_minute": None,
            "max_human_review_rate": None,
            "max_ocr_cer": None,
            "max_ocr_wer": None,
            "max_p95_latency_seconds": None,
        }
        defaults.update(overrides)
        return BenchmarkConfig(**defaults)


if __name__ == "__main__":
    unittest.main()
