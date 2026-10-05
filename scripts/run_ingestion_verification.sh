#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
compose=(docker --context "${IDP_DOCKER_CONTEXT:-idp-staging}" compose -f infra/verification/compose.yml)

"${compose[@]}" up -d --no-build
restore_intake() {
  "${compose[@]}" up -d --no-build ingestion-service
}
trap restore_intake EXIT

# Prevent the background reconciler from consuming deliberately abandoned leases.
"${compose[@]}" stop ingestion-service
"${compose[@]}" run --rm --no-deps ingestion-service python -m scripts.verify_ingestion_recovery
restore_intake
"${compose[@]}" exec -T ingestion-service python -m scripts.verify_tenant_boundaries
