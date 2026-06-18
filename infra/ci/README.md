# CI/CD Milestone

The CI/CD milestone protects the production Compose baseline, publishes
release-candidate artifacts, deploys continuously to staging, and promotes to
production only through manual approval before any Kubernetes or Minikube
migration.

## GitHub Actions

Workflow: `.github/workflows/ci.yml`

It runs on pull requests, pushes to `main`, and manual dispatch.

Jobs:

- `python-contracts`: runs unit and contract tests for the public API, gateway
  contract, webhooks, pipeline contracts, benchmark logic, and lightweight
  service helpers.
- `compose-and-preflight`: validates the CI production-shaped env fixture,
  renders the local Compose config, and renders the production Compose overlay.
- `secret-hygiene`: fails if real `.env` or `.env.production` files are tracked.
- `docker-build-smoke`: builds the gateway-facing service images
  (`ingestion-service`, `workflow-orchestrator`, `delivery-service`) on `main`,
  manual dispatch, or PRs labeled `ci:docker`.

Additional workflows:

- `.github/workflows/release-candidate.yml`: after successful CI on `main`,
  publishes SHA-tagged GHCR images for the gateway-facing app services and
  uploads a release-candidate manifest.
- `.github/workflows/deploy-staging.yml`: after successful release-candidate
  publication, deploys the SHA-tagged images to the GitHub `staging`
  environment over SSH and runs post-deploy smoke when staging URL/API key
  secrets are configured.
- `.github/workflows/deploy-production.yml`: manually promotes a SHA-tagged
  release candidate to the GitHub `production` environment after approval,
  production preflight, optional predeploy backup, and post-deploy smoke.
- `.github/workflows/rollback-production.yml`: manually rolls production back
  to a known-good image tag through the same production approval gate.

## CI Environment Fixture

`infra/ci/production.env` is intentionally fake. It exists only to prove that
production preflight and Compose rendering accept a hardened configuration shape.

Do not deploy it.

## Release Discipline

Production deployment is now manual-approval CD. It should be enabled only with
GitHub `production` environment reviewers and after staging smoke, backups,
rollback, and alerting have produced acceptable evidence.

Next CI/CD hardening steps:

- Add Ruff once the existing tree has a committed lint baseline.
- Run and archive staging backup, restore verification, rollback, and alert
  drills from `infra/staging/OPERATIONAL_READINESS.md`.
- Add image vulnerability scanning before production promotion.
- Automate isolated restore verification for production backups.
