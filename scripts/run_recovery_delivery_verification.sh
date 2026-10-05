#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker --context "${IDP_DOCKER_CONTEXT:-idp-staging}" compose -f infra/verification/compose.yml)
restore() { "${compose[@]}" up -d --no-build ocr-service delivery-service; }
trap restore EXIT
"${compose[@]}" up -d --no-build
"${compose[@]}" restart ingestion-service human-review-console workflow-orchestrator delivery-service
"${compose[@]}" stop ocr-service
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_recovery_delivery prepare
"${compose[@]}" start ocr-service
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_recovery_delivery reprocess
"${compose[@]}" stop delivery-service
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_recovery_delivery delivery
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_recovery_delivery prepare-reconcile
"${compose[@]}" start delivery-service
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_recovery_delivery finish
