# Staging corpus and Docker context

## Bootstrap corpus

Generate a reproducible local corpus with the repository virtual environment:

```bash
.venv/bin/python scripts/generate_staging_gold_set.py \
  --output "$PWD/artifacts/staging/gold-set-bootstrap"
```

The generated `manifest.jsonl` contains 20 distinct documents: seven invoices,
seven receipts, six purchase orders, five two-page TIFFs and 15 single-page PNGs.
All documents have generator-derived field and OCR labels, content hashes, and
explicit synthetic provenance. Totals on the second page exercise aggregation.
There are no customer documents or externally licensed images in this set.

This is a functional bootstrap corpus, NOT a representative production gold set.
It lacks natural scan defects, handwriting, PDFs, varied vendors and independent
annotation. Do not use its results to approve production accuracy. The manifest
preflight rejects synthetic samples when the acceptance policy is `approved`.
Add permissioned real examples and reviewed annotations for release acceptance.

For the current checkout the generated manifest is:
`/Users/user/Projects/intelligent_document_processing/artifacts/staging/gold-set-bootstrap/manifest.jsonl`.
For a persistent runner path, rerun the generator with an output directory outside
the Actions checkout, then configure that absolute manifest path as the staging
GitHub environment variable `STAGING_BENCHMARK_MANIFEST`. Absolute image paths
are machine-specific; regenerate after moving the corpus.

## Explicit local Docker context

The local `idp-staging` context targets
`unix:///Users/user/.orbstack/run/docker.sock`. It does not start containers or
create an isolated Docker daemon. Staging must use its own Compose project and
volumes; do not run global pruning on this shared engine.

Recreate on another runner (substitute its actual running engine socket):

```bash
docker context create idp-staging \
  --description 'IDP staging on OrbStack' \
  --docker "host=unix://${HOME}/.orbstack/run/docker.sock"
docker --context idp-staging info
docker --context idp-staging ps --filter label=com.docker.compose.project=idp-staging-stack
```

No `docker context use` is needed. Pass `--context idp-staging` explicitly during
manual operations. From the deployed release directory, inspect the stack with:

```bash
docker --context idp-staging compose -p idp-staging-stack \
  --env-file .env.staging -f docker-compose.yml \
  -f docker-compose.prod.yml -f docker-compose.staging.yml ps
```

The deployment workflow uses a temporary `DOCKER_CONFIG` for registry credentials,
so it cannot rely on contexts stored in the user's normal Docker configuration.
Set the staging GitHub variable `STAGING_DOCKER_HOST` to
`unix:///Users/user/.orbstack/run/docker.sock` for this runner. The existing workflow
already honors that variable. A named context does not fix absent containers;
run the normal staging deployment after configuring the endpoint. Persistent
production infrastructure must not share this local development engine.
