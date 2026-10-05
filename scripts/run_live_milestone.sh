#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker --context "${IDP_DOCKER_CONTEXT:-idp-staging}" compose -f infra/verification/compose.yml)
mkdir -p artifacts/live-milestone
# Build once: all application services share this verification image.
"${compose[@]}" build ingestion-service
"${compose[@]}" up -d --no-build
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_live_milestone prepare
docker --context "${IDP_DOCKER_CONTEXT:-idp-staging}" inspect --format '{{json .State}}' idp-live-verification-workflow-orchestrator-1 > artifacts/live-milestone/worker-before-crash.json
"${compose[@]}" kill -s SIGKILL workflow-orchestrator
"${compose[@]}" up -d --no-build workflow-orchestrator
docker --context "${IDP_DOCKER_CONTEXT:-idp-staging}" inspect --format '{{json .State}}' idp-live-verification-workflow-orchestrator-1 > artifacts/live-milestone/worker-after-crash.json
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_live_milestone finish
trap '"${compose[@]}" start ocr-service >/dev/null' EXIT
"${compose[@]}" stop ocr-service
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_live_milestone outage
"${compose[@]}" start ocr-service
trap - EXIT
cat artifacts/live-milestone/report.json
