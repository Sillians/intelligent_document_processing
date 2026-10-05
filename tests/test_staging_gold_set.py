import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from scripts.generate_staging_gold_set import generate
from scripts.validate_benchmark_manifest import validate


class GoldSetTests(unittest.TestCase):
    def test_generated_coverage_pages_and_approval_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate(Path(directory))
            policy = json.loads(Path("infra/staging/release_acceptance.json").read_text())
            self.assertTrue(validate(manifest, policy, 20)["passed"])
            rows = [json.loads(line) for line in manifest.read_text().splitlines()]
            self.assertEqual(len(rows), 20)
            for row in rows:
                with Image.open(row["image_path"]) as image:
                    self.assertEqual(getattr(image, "n_frames", 1), row["expected_page_count"])
            policy["status"] = "approved"
            self.assertFalse(validate(manifest, policy, 20)["passed"])
