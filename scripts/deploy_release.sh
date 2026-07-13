#!/usr/bin/env bash
set -euo pipefail

: "${IDP_IMAGE_REGISTRY:?set IDP_IMAGE_REGISTRY}"
: "${IDP_IMAGE_TAG:?set IDP_IMAGE_TAG}"
: "${IDP_ENV_FILE:=.env.production}"

if ! [[ "${IDP_IMAGE_TAG}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "IDP_IMAGE_TAG must be an immutable 40-character commit SHA." >&2
  exit 1
fi

compose=(
  docker compose
  --env-file "${IDP_ENV_FILE}"
  -f docker-compose.yml
  -f docker-compose.prod.yml
  -f docker-compose.release.yml
)
release_services=(
  ingestion-service
  workflow-orchestrator
  preprocess-worker
  ocr-service
  layout-service
  classifier-router-service
  extraction-service
  validation-service
  human-review-console
  delivery-service
  evaluation-service
  mlflow
)

"${compose[@]}" config --quiet
"${compose[@]}" pull

for service in "${release_services[@]}"; do
  image="${IDP_IMAGE_REGISTRY}/${service}:${IDP_IMAGE_TAG}"
  revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${image}")"
  if [ "${revision}" != "${IDP_IMAGE_TAG}" ]; then
    echo "Image revision mismatch for ${service}: expected ${IDP_IMAGE_TAG}, found ${revision:-missing}." >&2
    exit 1
  fi
done

"${compose[@]}" up -d --no-build --wait --wait-timeout 600
"${compose[@]}" ps
printf '%s\n' "${IDP_IMAGE_TAG}" > .idp-release-sha
