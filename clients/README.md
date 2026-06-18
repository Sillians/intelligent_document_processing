# IDP Client API

Client/API consumers should access the IDP through the API gateway. In local
development, that gateway is Traefik:

```text
http://localhost:8081
```

Do not point clients at the raw `ingestion-service` port for normal testing.
That direct port is only for internal/debug use. Production clients should use
the real HTTPS gateway or load balancer URL, for example:

```text
https://idp.yourcompany.com
```

## Public Endpoints

```http
GET /
GET /health
POST /documents
GET /documents/{job_id}
GET /documents/{job_id}/result
```

Opening `http://localhost:8081/` returns a small JSON API index. Use the
specific document endpoints for client integrations.

## Required Headers

```http
X-API-Key: <tenant-api-key>
X-Tenant-Id: <tenant-id>
X-Actor-Id: <user-or-system-id>
Idempotency-Key: <stable-client-request-id>
X-Request-ID: <optional-client-correlation-id>
```

`X-Actor-Id` and `Idempotency-Key` are required for `POST /documents`.
Polling and result fetches require `X-API-Key` and `X-Tenant-Id`.

## Submit A Document Locally

```bash
curl -X POST "http://localhost:8081/documents" \
  -H "X-API-Key: dev-ingestion-key" \
  -H "X-Tenant-Id: default" \
  -H "X-Actor-Id: local-client" \
  -H "Idempotency-Key: local-upload-001" \
  -F "file=@/absolute/path/to/document.png"
```

The response includes a `job_id` and `status_url`.


## Poll Status

```bash
curl "http://localhost:8081/documents/<job_id>" \
  -H "X-API-Key: dev-ingestion-key" \
  -H "X-Tenant-Id: default"
```

**output:**
```bash
curl -X GET "http://localhost:8081/documents/9de4164a-d6ca-4339-958d-b14bc1fcb698" \
     -H "X-API-Key: dev-ingestion-key" \
     -H "X-Tenant-Id: default" \
     -H "X-Actor-Id: local-client"
{"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","tenant_id":"default","status":"COMPLETED","source":"api","filename":"IDP-workflow-Gartner.png","content_type":"image/png","size_bytes":27016,"artifact_uri":"s3://raw-documents/jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/raw/IDP-workflow-Gartner.png","workflow_id":"idp-default-9de4164a-d6ca-4339-958d-b14bc1fcb698","workflow_run_id":"019ecc0f-2ab2-798a-93ae-4235e0fcc413","workflow_status":"COMPLETED","error_code":null,"error_message":null,"created_at":"2026-06-15T16:13:25.971014+00:00","updated_at":"2026-06-15T16:13:26.636482+00:00","metadata":{"submitted_by":"local-client","external_reference":null,"ingestion_auth_scheme":"api-key"}}%                                                                            
```


## Fetch Final Result

```bash
curl "http://localhost:8081/documents/<job_id>/result" \
  -H "X-API-Key: dev-ingestion-key" \
  -H "X-Tenant-Id: default"
```

**output**
```bash
curl -X GET "http://localhost:8081/documents/9de4164a-d6ca-4339-958d-b14bc1fcb698/result" \
     -H "X-API-Key: dev-ingestion-key" \
     -H "X-Tenant-Id: default" \
     -H "X-Actor-Id: local-client"
{"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","result":{"classification":{"auto_route":false,"classification_bucket":"layout-artifacts","classification_confidence":0.35,"classification_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/classification/route.json","confidence_band":"low","extraction_mode":"layout_aware_vlm","job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","matched_signal_count":0,"requires_review":true,"route":"generic","strategy_profile":"generic_v1"},"extraction":{"confidence":0.19,"extraction_bucket":"extraction-artifacts","extraction_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/extraction/result.json","fields":{"document_date":"","primary_party":"","secondary_party":"","summary":"ocr_fallback:forced_fallback","total_amount":""},"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","used_vlm_fallback":false},"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","layout":{"backend":"heuristic","block_count":1,"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","layout_bucket":"layout-artifacts","layout_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/layout/layout.json","text_zone_count":0},"ocr":{"fallback_used":true,"full_text":"ocr_fallback:forced_fallback","job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","mean_confidence":0.2,"ocr_bucket":"ocr-artifacts","ocr_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/ocr/ocr.json","token_count":1},"preprocess":{"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","metadata":{"deskew_angle":0.0,"duration_ms":1936.954,"foreground_pixels":193012,"height":647,"median_blur_kernel":3,"original_height":647,"original_width":1280,"pipeline":["resize_skipped","grayscale","denoise","clahe","deskew_skipped","adaptive_threshold","median_blur"],"resize_scale":1.0,"threshold_block_size":35,"width":1280},"preprocessed_bucket":"preprocessed-documents","preprocessed_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/preprocessed/page-0001.png"},"review_task":{"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","priority":"high","provider":"local_queue","review_bucket":"review-artifacts","review_ref":"review-461e89669dc1d6fe2dffaa67","review_status":"queued_without_label_studio","review_task_id":"review-461e89669dc1d6fe2dffaa67","review_task_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/review/review-461e89669dc1d6fe2dffaa67.json"},"status":"pending_human_review","validation":{"confidence":0.19,"duration_ms":1558.904,"fields":{"document_date":"","primary_party":"","secondary_party":"","summary":"ocr_fallback:forced_fallback","total_amount":""},"job_id":"9de4164a-d6ca-4339-958d-b14bc1fcb698","policy_version":"2026-06-03","reasons":["low_field_confidence:summary","low_confidence:0.190"],"requires_human_review":true,"route":"generic","rule_results":[{"failed_fields":[],"fields":["summary"],"rule":"required_fields_present","status":"passed"},{"failed_fields":["document_date","primary_party","secondary_party","total_amount"],"fields":["document_date","primary_party","secondary_party","total_amount"],"rule":"recommended_fields_present","status":"warning"},{"failed_fields":[],"fields":["document_date"],"rule":"date_format","status":"passed"},{"failed_fields":[],"fields":["total_amount"],"rule":"money_format","status":"passed"},{"failed_fields":["summary"],"rule":"required_field_confidence","status":"failed","threshold":0.6},{"actual":0.19,"rule":"document_confidence","status":"failed","threshold":0.9},{"rule":"llm_fallback_policy","status":"passed","used_vlm_fallback":false}],"source":{"artifact_loaded":true,"extraction_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/extraction/result.json"},"used_vlm_fallback":false,"validation_bucket":"validation-artifacts","validation_key":"jobs/9de4164a-d6ca-4339-958d-b14bc1fcb698/validation/result.json","validation_profile":"default","verdict":"needs_review","warnings":["missing_recommended_fields:document_date,primary_party,secondary_party,total_amount"]}}}%                                                                                                     
```

## Python Reference Client

The dependency-free Python client defaults can target the local gateway:

```bash
python3 clients/python/idp_client.py \
  samples/documents/sample_invoice_001.png \
  --base-url http://localhost:8081 \
  --api-key dev-ingestion-key \
  --tenant-id default \
  --actor-id local-client \
  --idempotency-key sample-invoice-001
```

- `http://localhost:8081/` now returns a small JSON API index:
```sh
{
 {
  "service": "intelligent-document-processing-api",
  "status": "ok",
  "base_url_usage": "Use this gateway base URL with the public document endpoints.",
  "endpoints": {
    "health": "/health",
    "submit_document": "POST /documents",
    "poll_status": "GET /documents/{job_id}",
    "fetch_result": "GET /documents/{job_id}/result"
  },
  "documentation": "infra/api/PUBLIC_API.md"
}
```

```bash
curl http://localhost:8081/
{
    {
    "service":"intelligent-document-processing-api",
    "status":"ok",
    "base_url_usage":"Use this gateway base URL with the public document endpoints.",
    "endpoints":{
        "health":"/health",
        "submit_document":"POST /documents",
        "poll_status":"GET /documents/{job_id}",
        "fetch_result":"GET /documents/{job_id}/result"
    },
"documentation":"infra/api/PUBLIC_API.md"}%   
```


- `Base/API index`: http://localhost:8081/
- `Health`: http://localhost:8081/health
- `Submit`: POST http://localhost:8081/documents
- `Poll`: GET http://localhost:8081/documents/{job_id}
- `Result`: GET http://localhost:8081/documents/{job_id}/result


The full public contract, including error format, retries, idempotency, and
webhooks, is documented in `infra/api/PUBLIC_API.md`.
