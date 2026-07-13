# GitHub Automation

This directory contains the repository automation for the current CI/CD
milestone. The goal is to protect the production Docker Compose baseline,
publish release-candidate artifacts, deploy continuously to staging, and promote
to production only through manual approval before adding Kubernetes or Minikube.

## Workflows

### IDP CI

File: `.github/workflows/ci.yml`

**Triggers:**

- Pull requests.
- Pushes to `main`.
- Manual `workflow_dispatch` runs.

Concurrency is enabled per branch/ref, so newer runs cancel older in-progress
runs for the same workflow and ref.

Permissions are read-only by default:

```yaml
permissions:
  contents: read
```

### Release Candidate

File: `.github/workflows/release-candidate.yml`

**Triggers:**

- Successful `IDP CI` runs on `main`.
- Manual `workflow_dispatch` runs.

This workflow publishes immutable, SHA-tagged staging release images to GHCR:

- `ghcr.io/<owner>/<repo>/ingestion-service:<sha>`
- `ghcr.io/<owner>/<repo>/workflow-orchestrator:<sha>`
- `ghcr.io/<owner>/<repo>/preprocess-worker:<sha>`
- `ghcr.io/<owner>/<repo>/ocr-service:<sha>`
- `ghcr.io/<owner>/<repo>/layout-service:<sha>`
- `ghcr.io/<owner>/<repo>/classifier-router-service:<sha>`
- `ghcr.io/<owner>/<repo>/extraction-service:<sha>`
- `ghcr.io/<owner>/<repo>/validation-service:<sha>`
- `ghcr.io/<owner>/<repo>/human-review-console:<sha>`
- `ghcr.io/<owner>/<repo>/delivery-service:<sha>`
- `ghcr.io/<owner>/<repo>/evaluation-service:<sha>`
- `ghcr.io/<owner>/<repo>/mlflow:<sha>`

It also updates the moving `staging-candidate` tag for each image and uploads a
small release-candidate manifest artifact. SHA tags are write-once: publication
fails if any `<service>:<sha>` already exists, while only
`staging-candidate` is intentionally mutable.

Release-candidate images are published as `linux/amd64`, except
`ocr-service`, which is published for `linux/amd64` and `linux/arm64` so the
Apple Silicon staging runner can use a native CPU image. For staging,
`ocr-service` uses the compact Tesseract dependency profile and
`layout-service` uses the lightweight OpenCV/preprocess profile instead of
shipping the full PaddleOCR, Torch, and LayoutParser runtimes:

- `ocr-service` runs real CPU OCR with `OCR_BACKEND=tesseract` and
  `OCR_FORCE_FALLBACK=false`.
- `layout-service` uses its built-in heuristic backend when LayoutParser /
  Detectron2 is unavailable.

The push CI Docker smoke builds the CPU OCR image and runs Tesseract against the
sample invoice. The OCR container health check uses `/ready`, so a missing
Tesseract binary or failed engine initialization prevents staging deployment.

This keeps the local Apple Silicon staging runner practical. Full PaddleOCR and
Detectron2 images should be reserved for a larger production-grade runner or a
dedicated heavyweight staging profile.

### Deploy Staging

File: `.github/workflows/deploy-staging.yml`

**Triggers:**

- Successful `Release Candidate` runs on `main`.
- Manual `workflow_dispatch` runs with an optional image tag.

This workflow deploys the release candidate to the GitHub `staging` environment.
Use environment protection rules if staging deploys should require approval.

Before copying the release into `${STAGING_APP_DIR}`, the workflow verifies that
every required service image exists in GHCR under the immutable SHA tag. If the
run fails with missing images such as:

```text
ghcr.io/<owner>/<repo>/preprocess-worker:<sha>: not found
```

the SHA has not been fully published by `Release Candidate`. Run
`Release Candidate` for that exact commit SHA first, or rerun `Deploy Staging`
with an `image_tag` copied from a successful `release-candidate-manifest`
artifact. Do not use `staging-candidate` for validation or promotion evidence;
it is intentionally mutable.

For manual deploys, `image_tag` is also used as the checked-out source ref. This
keeps the compose files, deploy scripts, and container images aligned to the same
immutable revision.

### Benchmark Staging

File: `.github/workflows/benchmark-staging.yml`

**Trigger:**

- Manual `workflow_dispatch` with sample count and concurrency.

This workflow runs CORD-v2 against the live staging gateway using
`infra/staging/release_acceptance.json`. It records CER/WER, extraction F1,
validation accuracy, throughput, and p50/p95 latency, fails when an acceptance
threshold is missed, and uploads the complete benchmark evidence directory. It
shares the staging deployment concurrency group so a benchmark cannot overlap a
redeployment. It reads `.idp-release-sha` from the deployed stack and checks out
that exact source revision.

### Harden Staging

File: `.github/workflows/harden-staging.yml`

This manual workflow validates credential age, tenant isolation, transport,
audit persistence, encrypted backup integrity, and retention enforcement. It
requires a second staging tenant/key and shares the staging concurrency group.

### Deploy Production

File: `.github/workflows/deploy-production.yml`

**Trigger:**

- Manual `workflow_dispatch` only.

The verification job first downloads the selected `Benchmark Staging` artifact
and proves it passed the approved policy for the requested 40-character release
SHA. Only then does the protected `production` environment request approval.
The deployment installs TLS material, deploys every release service, and checks
each image's OCI revision label before startup.

### Rollback Production

File: `.github/workflows/rollback-production.yml`

**Trigger:**

- Manual `workflow_dispatch` only.

This workflow rolls production back to a previously known-good release SHA through
the same GitHub `production` environment approval gate.

## Dependabot

File: `.github/dependabot.yml`

Dependabot checks GitHub Actions in the repository root and Docker base images
in `/docker`. Both managed Dockerfiles live in `/docker`; do not add a separate
root Docker updater unless a root-level Dockerfile or Kubernetes manifest is
introduced. Docker Compose files alone are not discovered by this updater and
an empty root target fails with `dependency_file_not_found`.

## CI Jobs

### `python-contracts`

Runs the fast Python contract suite with Python `3.11` and `uv`.

**Coverage includes:**

- production preflight unit tests,
- public API and client contract tests,
- full-pipeline result contract tests,
- dataset benchmark logic tests,
- workflow/orchestrator payload tests,
- webhook/delivery tests,
- gateway-facing service helper tests,
- lightweight OCR/layout/preprocess helper tests.

The workflow installs only the dependency groups needed for these checks:

```bash
uv sync --frozen --no-default-groups \
  --group temporal \
  --group preprocess \
  --group extraction \
  --group evaluation \
  --group research
```

### `compose-and-preflight`

Validates production behavior without booting the stack:

- runs `scripts/production_preflight.py` against `infra/ci/production.env`,
- renders the local Compose config,
- renders the production Compose overlay,
- checks the Python service and MLflow Dockerfile build definitions.

The CI env file is intentionally fake but production-shaped. It proves that
preflight and Compose rendering accept a hardened configuration shape.

### `secret-hygiene`

Fails the workflow if real local env files are tracked:

- `.env`
- `.env.production`

It also reruns production preflight against the CI env fixture.

### `docker-build-smoke`

Builds the gateway-facing service images:

- `ingestion-service`
- `workflow-orchestrator`
- `delivery-service`

**This job runs on:**

- pushes to `main`,
- manual workflow dispatch,
- pull requests labeled `ci:docker`.

It does not run on every pull request by default because the full stack has
large optional OCR/layout/GPU dependencies and the project is still optimizing
image size and build time.

## Release Candidate Artifacts

Release candidate images are published to GitHub Container Registry using the
commit SHA as the immutable tag. The staging deployment consumes those SHA tags,
not mutable branch names.

The staging Compose overlay is:

```text
docker-compose.staging.yml
```

It points the functional pipeline services at release-candidate images through:

```text
IDP_IMAGE_REGISTRY
IDP_IMAGE_TAG
```

The staging deploy pulls and starts:

- `postgres`, `redis`, `seaweedfs`, `temporal`, `gateway`, `mlflow`, and `label-studio`.
- `ingestion-service`, `workflow-orchestrator`, `preprocess-worker`, `ocr-service`, `layout-service`, `classifier-router-service`, `extraction-service`, `validation-service`, `human-review-console`, `delivery-service`, and `evaluation-service`.

The deploy creates missing Postgres databases for reused staging volumes:

- the configured `POSTGRES_DB`,
- `temporal`,
- `temporal_visibility`,
- `mlflow`,
- `labelstudio`.

After application health checks, deployment calls
`human-review-console /health/provider`. When Label Studio is selected, this
must authenticate successfully and resolve `LABEL_STUDIO_PROJECT_ID`; otherwise
deployment fails with both integration and Label Studio logs.

It then waits for all HTTP application services to become healthy before running
the post-deploy full-pipeline smoke test.

The production release image overlay is:

```text
docker-compose.release.yml
```

It intentionally does not set a Compose project name. Production keeps the
`idp-production-stack` project name from `docker-compose.prod.yml`.

## Staging CD

Staging deploys run on a self-hosted GitHub Actions runner installed on the
staging machine. This avoids buying a public domain and avoids SSH/Cloudflare
Access from GitHub-hosted runners.

Register the runner under:

```text
GitHub repository -> Settings -> Actions -> Runners -> New self-hosted runner
```

Add a custom runner label:

```text
staging
```

The staging job uses:

```yaml
runs-on: [self-hosted, staging]
```

Required GitHub environment/secrets for `staging`:

- `STAGING_ENV_FILE`: complete `.env.staging` contents.

When changing staging values such as `GATEWAY_HTTP_HOST_PORT`, update the
GitHub `STAGING_ENV_FILE` secret with the full new `.env.staging` contents.
Changing only the local `.env.staging` file does not affect GitHub Actions.
For example, if a local development stack already uses `8081`, set this inside
the GitHub secret:

```text
GATEWAY_HTTP_HOST_PORT=8089
```

The CPU OCR, bounded deskew, and Label Studio integration profile requires
these values inside `STAGING_ENV_FILE`:

```text
PREPROCESS_DESKEW_MAX_ANGLE=15.0
OCR_BACKEND=tesseract
OCR_FORCE_FALLBACK=false
OCR_TESSERACT_OEM=1
OCR_TESSERACT_PSM=3
LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN=true
LABEL_STUDIO_AUTH_SCHEME=token
STAGING_REVIEW_PROVIDER=label_studio
```

Use `LABEL_STUDIO_AUTH_SCHEME=pat` instead when
`LABEL_STUDIO_TOKEN` is a personal access token rather than the bootstrapped
legacy `LABEL_STUDIO_USER_TOKEN`.

**Optional staging configuration:**

- environment variable `STAGING_APP_DIR`: local deploy path on the staging
  runner. Defaults to `$HOME/idp-staging` on macOS self-hosted runners and
  `/opt/idp-staging` on Linux runners.
- `STAGING_PUBLIC_BASE_URL`: public or local staging gateway URL for post-deploy smoke.
- `STAGING_SMOKE_API_KEY`: API key used by post-deploy smoke.
- environment variable `STAGING_TENANT_ID`: tenant for smoke tests, defaults to `default`.
- `STAGING_PROMETHEUS_URL`: optional Prometheus URL. When present, smoke tests
  also require live IDP scrape targets.

For the local self-hosted staging runner path, do not use placeholder domains
such as `https://staging-idp.example.com`. Use the gateway port exposed on the
runner host:

```text
STAGING_PUBLIC_BASE_URL=http://127.0.0.1:8081
```

Leave `STAGING_PROMETHEUS_URL` unset unless the staging deploy also starts a
reachable Prometheus instance. If observability is running locally, set it to
the reachable local URL, for example:

```text
STAGING_PROMETHEUS_URL=http://127.0.0.1:9090
```

**Docker Desktop storage for full-pipeline staging:**

Docker Desktop stores image layers inside its own Linux VM disk under paths such
as `/var/lib/desktop-containerd`. A deploy can fail even when macOS still has
free disk space:

```text
no space left on device
failed to extract layer ... /var/lib/desktop-containerd/...
```

The staging workflow prunes stopped containers, build cache, and unused images
before pulling release images. It does not prune volumes, so staging Postgres,
SeaweedFS, MLflow, and Label Studio data are preserved. To disable this cleanup,
set the GitHub staging environment variable:

```text
STAGING_DOCKER_PRUNE_BEFORE_PULL=false
```

If cleanup is not enough, increase Docker Desktop's virtual disk limit:

```text
Docker Desktop -> Settings -> Resources -> Advanced -> Virtual disk limit
```

For the lightweight staging profile, budget at least `40GB` for Docker Desktop.
If you switch staging back to full PaddleOCR/LayoutParser images, budget at
least `80GB` because individual ML runtime layers can exceed `5GB`.

The following SSH/Cloudflare values are not required for the self-hosted staging
path:

```text
STAGING_HOST
STAGING_USER
STAGING_PORT
STAGING_SSH_KEY
STAGING_KNOWN_HOSTS
STAGING_USE_CLOUDFLARE
```

**Prepare the staging machine:**

Create the deployment directory and give the runner user ownership.

For macOS with Docker Desktop, prefer a path under the runner user's home
directory because Docker Desktop only bind-mounts files from shared host
locations. The workflow default is `$HOME/idp-staging` on macOS.

```bash
mkdir -p "$HOME/idp-staging/releases"
chmod -R u+rwX "$HOME/idp-staging"
```

If you override `STAGING_APP_DIR` to a path such as `/opt/idp-staging` on
macOS, add that path in Docker Desktop:

```text
Docker Desktop -> Settings -> Resources -> File Sharing
```

Otherwise the deploy step can fail with:

```text
mounts denied: The path /opt/idp-staging/... is not shared from the host and is not known to Docker
```

For Linux runners, `/opt/idp-staging` is a reasonable default. Replace
`github-runner` with the actual OS user running the GitHub Actions runner.

```bash
sudo mkdir -p /opt/idp-staging/releases
sudo chown -R github-runner:github-runner /opt/idp-staging
chmod -R u+rwX /opt/idp-staging
```

Install and verify Docker Compose for the runner user:

```bash
docker version
docker compose version
docker ps
```

If `docker ps` fails with a permission error on Linux, add the runner user to
the Docker group and restart the runner service:

```bash
sudo usermod -aG docker github-runner
```

For macOS with Docker Desktop, ensure Docker Desktop is running before the
self-hosted runner starts and that `STAGING_APP_DIR` is inside a shared path.
The workflow writes a temporary `DOCKER_CONFIG` with a GHCR auth entry instead
of calling `docker login`. This avoids Docker Desktop's macOS Keychain
credential helper, which can fail in non-interactive runner jobs with:

```text
error saving credentials: error storing credentials - err: exit status 1, out: `User interaction is not allowed. (-25308)`
```

Release Candidate images are published for `linux/amd64`. Docker Desktop on
Apple Silicon can run them through emulation. The staging OCR/layout images are
kept lightweight to avoid pulling multi-gigabyte PaddleOCR/Torch layers during
local staging deploys.

**Staging deployment behavior:**

- copies the checked-out release into `${STAGING_APP_DIR}/releases/<sha>` on the self-hosted runner,
- atomically updates `${STAGING_APP_DIR}/current`,
- writes `.env.staging` from the encrypted GitHub secret,
- writes a temporary Docker GHCR auth config using the workflow token,
- validates Compose config,
- prunes stopped containers, Docker build cache, and unused images unless
  `STAGING_DOCKER_PRUNE_BEFORE_PULL=false`,
- pulls the full functional release-candidate image set,
- starts required infra dependencies: `postgres`, `redis`, `seaweedfs`,
  `gateway`, `temporal`, `mlflow`, and `label-studio`,
- ensures the configured `POSTGRES_DB`, `temporal`, `temporal_visibility`,
  `mlflow`, and `labelstudio` databases exist in the staging Postgres instance,
- starts all application services and waits for HTTP service health,
- runs the full-pipeline smoke through the staging gateway.

The staging overlay keeps infra dependency ports internal-only, so local
developer services on ports such as `5432`, `7233`, `8333`, or `6379` do not
collide with the staging stack. The externally exposed staging entrypoint is
the gateway port from `GATEWAY_HTTP_HOST_PORT` / `GATEWAY_HTTPS_HOST_PORT`.
- runs post-deploy smoke when staging URL and API key secrets are configured,
  then uploads the smoke artifacts.

Operational readiness drills are documented in:

```text
infra/staging/OPERATIONAL_READINESS.md
```

The ordered staging validation and production-promotion gates are documented in:

```text
infra/staging/DEPLOYMENT_VALIDATION.md
```

## Production CD

Production CD is manual-approval CD. It is never triggered automatically by a
push, CI run, or staging deploy.

Configure `Settings > Environments > production` before the first promotion:

- add at least one required reviewer,
- enable `Prevent self-review`,
- restrict deployment branches to `main`,
- disable environment-rule bypass,
- store production secrets only on this environment, not as staging secrets.

The workflow references `environment: production`, but required reviewers are a
repository setting and cannot be safely self-configured by the deployment job.
See [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

Required GitHub environment/secrets for `production`:

- `PRODUCTION_HOST`: SSH host or IP.
- `PRODUCTION_USER`: SSH user.
- `PRODUCTION_SSH_KEY`: private SSH key for that user.
- `PRODUCTION_KNOWN_HOSTS`: pinned SSH host identity; fallback key scanning is not allowed.
- `PRODUCTION_ENV_FILE`: complete `.env.production` contents.
- `PRODUCTION_TLS_CERT`: production certificate chain.
- `PRODUCTION_TLS_KEY`: production certificate private key.
- `PRODUCTION_PUBLIC_BASE_URL`: public production gateway URL for smoke tests.
- `PRODUCTION_SMOKE_API_KEY`: API key used by production smoke tests.
- `PRODUCTION_PROMETHEUS_URL`: Prometheus URL used to prove observability.

**Optional production configuration:**

- `PRODUCTION_PORT`: SSH port, defaults to `22`.
- environment variable `PRODUCTION_APP_DIR`: remote deploy path, defaults to `/opt/idp`.
- environment variable `PRODUCTION_TENANT_ID`: tenant for smoke tests, defaults to `default`.

**Production promotion behavior:**

- requires a 40-character `release_sha` and passing `staging_benchmark_run_id`,
- verifies the benchmark SHA, approved policy, sample count, and every quality gate before requesting approval,
- waits for required reviewers on the GitHub `production` environment,
- uploads a source bundle to `${PRODUCTION_APP_DIR}/releases/<release_sha>`,
- atomically updates `${PRODUCTION_APP_DIR}/current`,
- writes `.env.production` and TLS files from protected environment secrets,
- runs production preflight and Compose config validation,
- optionally runs an encrypted predeploy backup and downloads its manifest,
- pulls and starts the complete pipeline release,
- verifies every release image's OCI revision equals `release_sha`,
- runs production smoke with required observability checks,
- uploads promotion, backup-manifest, and smoke artifacts.

**Production rollback behavior:**

- requires manual dispatch with a known-good `image_tag` and rollback reason,
- waits for the same GitHub `production` environment approval gate,
- installs the production env file,
- validates production preflight and Compose config,
- deploys the complete known-good release and verifies image revisions,
- runs production smoke with required observability checks,
- uploads rollback and smoke artifacts.

## Dependabot

File: `.github/dependabot.yml`

Dependabot checks:

- GitHub Actions versions,
- Dockerfile base image references under `docker/`.

## Supporting CI Files

- `infra/ci/production.env`: CI-only production-shaped env fixture.
- `infra/ci/README.md`: milestone overview and next CI/CD hardening steps.
- `infra/staging/OPERATIONAL_READINESS.md`: staging smoke, backup, restore
  verification, rollback, and alert drills.
- `.github/events.md`: GitHub event reference notes.

## Next Automation Steps

- Add Ruff once the repository has a committed lint baseline.
- Add manual approval rules to the GitHub `staging` environment if desired.
- Add image vulnerability scanning before production promotion.
- Add automated restore drills on isolated infrastructure.
