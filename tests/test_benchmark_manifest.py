import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.validate_benchmark_manifest import validate


class ManifestTests(unittest.TestCase):
    def test_valid_and_duplicate_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            document = root / "sample.png"
            document.write_bytes(b"fixture")
            row = {
                "sample_id": "one", "image_path": str(document),
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
                "provenance": {"source": "fixture", "usage_permission": "owned", "label_reviewer": "tester"},
                "document_family": "invoice", "expected_page_count": 1,
                "ground_truth": {"expected_fields": {"total": "1"}, "expected_route": "invoice"},
            }
            manifest = root / "manifest.jsonl"
            manifest.write_text(json.dumps(row) + "\n")
            self.assertTrue(validate(manifest, {}, 20)["passed"])
            self.assertFalse(validate(manifest, {'status':'approved'}, 20)['passed'])
            reviewed = {**row, 'synthetic':False, 'label_review_status':'approved'}
            manifest.write_text(json.dumps(reviewed))
            self.assertTrue(validate(manifest, {'status':'approved'}, 20)['passed'])
            manifest.write_text(json.dumps({**reviewed, 'synthetic':True}))
            self.assertFalse(validate(manifest, {'status':'approved'}, 20)['passed'])
            manifest.write_text(json.dumps(row) + "\n" + json.dumps({**row, "sample_id": "two"}))
            self.assertIn("duplicate document content", " ".join(validate(manifest, {}, 20)["errors"]))
            row.pop("provenance")
            manifest.write_text(json.dumps(row))
            self.assertFalse(validate(manifest, {}, 20)["passed"])

    def test_missing_relative_and_coverage(self):
        self.assertFalse(validate(Path("relative.jsonl"), {}, 20)["passed"])
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory).resolve() / "manifest.jsonl"
            manifest.write_text("")
            self.assertFalse(validate(manifest, {"thresholds": {"min_sample_count": 20}}, 20)["passed"])
            manifest.write_text("not json")
            self.assertFalse(validate(manifest, {}, 20)["passed"])


if __name__ == "__main__":
    unittest.main()
