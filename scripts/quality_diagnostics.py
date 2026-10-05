"""Labeled-only quality diagnostics; no invented validation or confidence labels."""
from collections import defaultdict
from statistics import mean


def quality_breakdown(samples: list[dict]) -> dict:
    families, fields, bins = defaultdict(list), defaultdict(list), defaultdict(list)
    for sample in samples:
        family = sample.get('document_family') or 'unknown'
        families[family].append(sample)
        matches = sample.get('matched_fields') or {}
        for name, matched in matches.items():
            fields[(family, name)].append((bool(matched), bool((sample.get('populated_fields') or {}).get(name))))
        confidence = sample.get('extraction_confidence')
        if matches and isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
            bins[min(9, int(confidence * 10))].append((confidence, all(matches.values())))
    family_metrics = {}
    for family, items in sorted(families.items()):
        labeled = [item['field_f1'] for item in items if item.get('matched_fields')]
        validation = [item['validation_correct'] for item in items if item.get('validation_correct') is not None]
        cer = [item['ocr_cer'] for item in items if item.get('ocr_cer') is not None]
        family_metrics[family] = {'sample_count': len(items), 'field_labeled_count': len(labeled),
            'field_f1_mean': mean(labeled) if labeled else None,
            'ocr_cer_mean': mean(cer) if cer else None, 'ocr_reference_count': len(cer),
            'validation_accuracy': mean(validation) if validation else None, 'validation_labeled_count': len(validation)}
    field_metrics = {}
    for (family, name), values in sorted(fields.items()):
        tp = sum(matched for matched, _ in values)
        fp = sum(populated and not matched for matched, populated in values)
        fn = len(values) - tp
        field_metrics.setdefault(family, {})[name] = {'labeled_count': len(values), 'tp': tp, 'fp': fp, 'fn': fn,
            'precision': tp / (tp + fp) if tp + fp else 0,
            'recall': tp / len(values), 'f1': 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0}
    calibration = []
    for index, values in sorted(bins.items()):
        calibration.append({'lower': index / 10, 'upper': (index + 1) / 10, 'count': len(values),
            'mean_confidence': mean(score for score, _ in values),
            'observed_document_exact_accuracy': mean(correct for _, correct in values)})
    count = sum(item['count'] for item in calibration)
    ece = sum(item['count'] * abs(item['mean_confidence'] - item['observed_document_exact_accuracy']) for item in calibration) / count if count else None
    return {'by_document_family': family_metrics, 'by_field': field_metrics,
            'document_confidence_calibration': {'labeled_count': count, 'ece': ece, 'bins': calibration,
             'scope': 'Document exact match over annotated positive fields, not per-field calibrated probability'}}
