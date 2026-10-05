"""Validate a runner-local gold set without contacting staging or publishing documents."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def validate(manifest: Path, policy: dict, limit: int) -> dict:
    errors = []
    families = Counter()
    ids, digests = set(), set()
    multipage = references = validation_labels = count = 0
    try:
        if not manifest.is_absolute():
            raise ValueError("manifest path must be absolute")
        rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
        if limit <= 0:
            raise ValueError("limit must be positive")
        for index, row in enumerate(rows[:limit], 1):
            count += 1
            try:
                if not isinstance(row, dict):
                    raise ValueError("row must be an object")
                if row.get("synthetic") and policy.get("status") == "approved":
                    raise ValueError("synthetic bootstrap samples cannot establish approved release acceptance")
                if policy.get('status') == 'approved' and (row.get('synthetic') is not False or row.get('label_review_status') != 'approved'):
                    raise ValueError('approved evidence requires explicit real-document provenance and approved independent labels')
                sample_id = row.get("sample_id")
                if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in ids:
                    raise ValueError("sample_id must be nonempty and unique")
                ids.add(sample_id)
                path = Path(row.get("image_path", ""))
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("image_path must be an existing absolute file path")
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                content_hash = digest.hexdigest()
                if content_hash in digests:
                    raise ValueError("duplicate document content")
                digests.add(content_hash)
                if row.get("sha256") != content_hash:
                    raise ValueError("sha256 must match document content")
                provenance = row.get("provenance")
                if not isinstance(provenance, dict) or not all(
                    isinstance(provenance.get(key), str) and provenance[key].strip()
                    for key in ("source", "usage_permission", "label_reviewer")
                ):
                    raise ValueError("provenance requires source, usage_permission, and label_reviewer")
                family = row.get("document_family")
                if not isinstance(family, str) or not family.strip():
                    raise ValueError("document_family is required")
                pages = row.get("expected_page_count")
                if type(pages) is not int or pages < 1:
                    raise ValueError("expected_page_count must be a positive integer")
                truth = row.get("ground_truth")
                if not isinstance(truth, dict) or not isinstance(truth.get("expected_fields"), dict) or not truth["expected_fields"]:
                    raise ValueError("nonempty ground_truth.expected_fields is required")
                if not isinstance(truth.get("expected_route"), str) or not truth["expected_route"].strip():
                    raise ValueError("ground_truth.expected_route is required")
                verdict = truth.get('expected_validation_verdict')
                if verdict and verdict not in {'auto_approved', 'needs_review', 'human_approved'}:
                    raise ValueError('Invalid expected_validation_verdict')
                validation_labels += bool(verdict)
                families[family] += 1
                multipage += pages > 1
                references += bool(str(truth.get("expected_ocr_text") or "").strip())
            except (ValueError, TypeError, OSError) as exc:
                errors.append(f"row {index}: {exc}")
        coverage = {
            "min_sample_count": count,
            "min_document_families": len(families),
            "min_samples_per_family": min(families.values(), default=0),
            "min_multi_page_samples": multipage,
            "min_ocr_reference_count": references,
            "min_validation_labeled_count": validation_labels,
        }
        for key, actual in coverage.items():
            required = policy.get("thresholds", {}).get(key, 0)
            if actual < required:
                errors.append(f"{key}: {actual} < {required}")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(str(exc))
    return {"passed": not errors, "errors": errors, "selected_samples": count,
            "document_families": dict(families),
            "note": "Declared labels and permissions require human approval; this check does not prove legal permission or document accuracy."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = validate(args.manifest, json.loads(args.policy.read_text()), args.limit)
    except (OSError, ValueError) as exc:
        report = {"passed": False, "errors": [str(exc)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
