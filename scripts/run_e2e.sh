#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
SAMPLE_PATH="${1:-samples/documents/sample_invoice_001.png}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INGESTION_API_KEY="${INGESTION_API_KEY:-dev-ingestion-key}"
TENANT_ID="${TENANT_ID:-default}"
STRICT_REQUIRED_FIELDS="${STRICT_REQUIRED_FIELDS:-0}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required but not installed" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "$PYTHON_BIN is required but not installed" >&2
  exit 1
fi

if [ ! -f "$SAMPLE_PATH" ]; then
  echo "Sample file not found at $SAMPLE_PATH. Generating one now."
  "$PYTHON_BIN" scripts/generate_sample_invoice.py --output "$SAMPLE_PATH"
fi

echo "Submitting document: $SAMPLE_PATH"
submit_response="$(curl -sS -X POST "${API_URL%/}/documents" \
  -H "X-API-Key: ${INGESTION_API_KEY}" \
  -H "X-Tenant-Id: ${TENANT_ID}" \
  -F "file=@${SAMPLE_PATH}")"

echo "Submission response:"
echo "$submit_response"

job_id="$(printf '%s' "$submit_response" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
workflow_id="$(printf '%s' "$submit_response" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("workflow_id",""))')"

echo "job_id=$job_id workflow_id=$workflow_id"

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
status="UNKNOWN"

while [ "$(date +%s)" -lt "$deadline" ]; do
  status_response="$(curl -sS "${API_URL%/}/documents/${job_id}" \
    -H "X-API-Key: ${INGESTION_API_KEY}" \
    -H "X-Tenant-Id: ${TENANT_ID}")"
  status="$(printf '%s' "$status_response" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("status","UNKNOWN"))')"
  echo "Workflow status: $status"

  if [ "$status" = "COMPLETED" ]; then
    break
  fi

  if [ "$status" = "FAILED" ] || [ "$status" = "TERMINATED" ] || [ "$status" = "CANCELED" ]; then
    echo "Workflow ended unsuccessfully: $status" >&2
    echo "$status_response" >&2
    exit 1
  fi

  sleep "$POLL_INTERVAL"
done

if [ "$status" != "COMPLETED" ]; then
  echo "Timeout after ${TIMEOUT_SECONDS}s waiting for workflow completion" >&2
  exit 1
fi

result_response="$(curl -sS "${API_URL%/}/documents/${job_id}/result" \
  -H "X-API-Key: ${INGESTION_API_KEY}" \
  -H "X-Tenant-Id: ${TENANT_ID}")"

RESULT_RESPONSE="$result_response" STRICT_REQUIRED_FIELDS="$STRICT_REQUIRED_FIELDS" "$PYTHON_BIN" - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["RESULT_RESPONSE"])
strict_required_fields = os.environ.get("STRICT_REQUIRED_FIELDS", "0").lower() in {"1", "true", "yes"}
result = payload.get("result", {})
fields = result.get("extraction", {}).get("fields", {})
final_status = result.get("status", "unknown")

required = ["invoice_number", "invoice_date", "total_amount"]
missing = [k for k in required if not fields.get(k)]

summary = {
    "job_id": payload.get("job_id"),
    "workflow_final_status": final_status,
    "ocr_confidence": result.get("ocr", {}).get("mean_confidence"),
    "extraction_confidence": result.get("extraction", {}).get("confidence"),
    "used_vlm_fallback": result.get("extraction", {}).get("used_vlm_fallback"),
    "requires_review": result.get("validation", {}).get("requires_human_review"),
    "missing_required_fields": missing,
    "strict_required_fields": strict_required_fields,
}

print("E2E summary:")
print(json.dumps(summary, indent=2))

if final_status not in {"delivered", "pending_human_review"}:
    print(f"Unexpected workflow final status: {final_status}", file=sys.stderr)
    sys.exit(1)

if missing and (strict_required_fields or final_status == "delivered"):
    print("Missing required fields in extraction result", file=sys.stderr)
    sys.exit(2)
PY

echo "E2E test completed successfully."
