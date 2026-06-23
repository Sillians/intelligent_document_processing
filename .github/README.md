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

This workflow publishes immutable, SHA-tagged gateway-facing images to GHCR:

- `ghcr.io/<owner>/<repo>/ingestion-service:<sha>`
- `ghcr.io/<owner>/<repo>/workflow-orchestrator:<sha>`
- `ghcr.io/<owner>/<repo>/delivery-service:<sha>`

It also updates the moving `staging-candidate` tag for each image and uploads a
small release-candidate manifest artifact.

### Deploy Staging

File: `.github/workflows/deploy-staging.yml`

**Triggers:**

- Successful `Release Candidate` runs on `main`.
- Manual `workflow_dispatch` runs with an optional image tag.

This workflow deploys the release candidate to the GitHub `staging` environment.
Use environment protection rules if staging deploys should require approval.

### Deploy Production

File: `.github/workflows/deploy-production.yml`

**Trigger:**

- Manual `workflow_dispatch` only.

This workflow promotes an immutable release-candidate image tag to the GitHub
`production` environment. Configure required reviewers on the `production`
environment in GitHub so the workflow pauses for human approval before touching
the server.

### Rollback Production

File: `.github/workflows/rollback-production.yml`

**Trigger:**

- Manual `workflow_dispatch` only.

This workflow rolls production back to a previously known-good image tag through
the same GitHub `production` environment approval gate.

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

It points `ingestion-service`, `workflow-orchestrator`, and `delivery-service`
at release-candidate images through:

```text
IDP_IMAGE_REGISTRY
IDP_IMAGE_TAG
```

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

**Optional staging configuration:**

- environment variable `STAGING_APP_DIR`: local deploy path on the staging
  runner. Defaults to `$HOME/idp-staging` on macOS self-hosted runners and
  `/opt/idp-staging` on Linux runners.
- `STAGING_PUBLIC_BASE_URL`: public or local staging gateway URL for post-deploy smoke.
- `STAGING_SMOKE_API_KEY`: API key used by post-deploy smoke.
- environment variable `STAGING_TENANT_ID`: tenant for smoke tests, defaults to `default`.
- `STAGING_PROMETHEUS_URL`: optional Prometheus URL. When present, smoke tests
  also require live IDP scrape targets.

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
The workflow uses a temporary `DOCKER_CONFIG` during GHCR login so Docker does
not try to write credentials into the macOS Keychain. Without that isolation,
non-interactive runner jobs can fail with:

```text
error saving credentials: error storing credentials - err: exit status 1, out: `User interaction is not allowed. (-25308)`
```

Release Candidate images are published for both `linux/amd64` and
`linux/arm64` so Apple Silicon self-hosted staging runners can pull native
images from GHCR.

**Staging deployment behavior:**

- copies the checked-out release into `${STAGING_APP_DIR}/releases/<sha>` on the self-hosted runner,
- atomically updates `${STAGING_APP_DIR}/current`,
- writes `.env.staging` from the encrypted GitHub secret,
- logs in to GHCR with the workflow token,
- validates Compose config,
- pulls the three release-candidate images,
- restarts only `gateway`, `ingestion-service`, `workflow-orchestrator`, and
  `delivery-service` with `--no-build --no-deps`.
- runs post-deploy smoke when staging URL and API key secrets are configured,
  then uploads the smoke artifacts.

Operational readiness drills are documented in:

```text
infra/staging/OPERATIONAL_READINESS.md
```

## Production CD

Production CD is manual-approval CD. It is never triggered automatically by a
push, CI run, or staging deploy.

Required GitHub environment/secrets for `production`:

- `PRODUCTION_HOST`: SSH host or IP.
- `PRODUCTION_USER`: SSH user.
- `PRODUCTION_SSH_KEY`: private SSH key for that user.
- `PRODUCTION_ENV_FILE`: complete `.env.production` contents.
- `PRODUCTION_PUBLIC_BASE_URL`: public production gateway URL for smoke tests.
- `PRODUCTION_SMOKE_API_KEY`: API key used by production smoke tests.
- `PRODUCTION_PROMETHEUS_URL`: Prometheus URL used to prove observability.

**Optional production configuration:**

- `PRODUCTION_PORT`: SSH port, defaults to `22`.
- `PRODUCTION_KNOWN_HOSTS`: pinned SSH known_hosts entry. If omitted, the workflow uses `ssh-keyscan`.
- environment variable `PRODUCTION_APP_DIR`: remote deploy path, defaults to `/opt/idp`.
- environment variable `PRODUCTION_TENANT_ID`: tenant for smoke tests, defaults to `default`.

**Production promotion behavior:**

- requires manual dispatch with an immutable `image_tag`,
- requires a `staging_evidence_url` pointing to approved staging smoke/readiness
  evidence,
- waits for the GitHub `production` environment approval gate,
- uploads a source bundle to `${PRODUCTION_APP_DIR}/releases/<image_tag>`,
- atomically updates `${PRODUCTION_APP_DIR}/current`,
- writes `.env.production` from the encrypted GitHub secret,
- runs production preflight and Compose config validation,
- optionally runs a predeploy backup drill and downloads its manifest,
- pulls the three release-candidate images,
- restarts only `gateway`, `ingestion-service`, `workflow-orchestrator`, and
  `delivery-service` with `--no-build --no-deps`,
- runs production smoke with required observability checks,
- uploads promotion, backup-manifest, and smoke artifacts.

**Production rollback behavior:**

- requires manual dispatch with a known-good `image_tag` and rollback reason,
- waits for the same GitHub `production` environment approval gate,
- installs the production env file,
- validates production preflight and Compose config,
- pulls the known-good images,
- restarts the gateway-facing services,
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
