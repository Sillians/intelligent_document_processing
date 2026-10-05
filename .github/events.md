The next likely major milestone should be **CI/CD and release discipline**.

You now have enough moving parts that manual testing is becoming risky: gateway, public API contract, webhooks, production preflight, Compose overlays, E2E scripts, benchmark artifacts, and multiple services. Before adding bigger features like OAuth/JWT, tenant admin UI, webhook retries dashboard, or Kubernetes, I’d lock in automated confidence.

Yes, you need GitHub Actions now.

Recommended milestone:

**Major Milestone: CI/CD For Production Readiness**

What it should include first:

1. **PR validation**
   - Run Python unit tests.
   - Run public API contract tests.
   - Run webhook contract tests.
   - Run pipeline/e2e contract tests that do not require the full Docker stack.
   - Run lint/type checks if already configured or easy to add.

2. **Compose validation**
   - `docker compose config --quiet`
   - production overlay config validation.
   - `python3 scripts/production_preflight.py --env-file .env.production.example` may intentionally fail because placeholders exist, so either add a CI-safe env fixture or a `--allow-placeholders` mode.

3. **Docker build checks**
   - Build service images in CI.
   - At minimum: `ingestion-service`, `workflow-orchestrator`, `delivery-service`, and gateway config validation.
   - Later: matrix build all services.

4. **Security checks**
   - Secret scanning.
   - Dependency vulnerability scan.
   - Dockerfile/container scan.
   - Ensure `.env.production` is never committed.

5. **Release artifact discipline**
   - Tag images by commit SHA.
   - Generate a deployment summary.
   - Keep production deployment manual at first, not auto-deploy.

I would not jump straight to full CD yet. Start with **CI + build + release candidate artifacts**. Once that is stable, add CD to a staging server. Production deploy should remain manual until you have backups, rollback, and monitoring alarms proven.

So the practical next milestone is:

> Every PR proves the public API, gateway contract, webhooks, production config shape, and Docker images are safe before merge.

After that, the next milestones become much cleaner:
- staging deployment
- OAuth/JWT/forward-auth
- tenant onboarding/API key management
- webhook consumer dashboard/replay
- Kubernetes/Minikube migration planning


---

You prove them with repeatable drills, not promises. The bar should be: “we can run this checklist, produce artifacts, and recover on purpose.”

**Staging Reliability**
Run the same release candidate through staging repeatedly and capture evidence.

Minimum proof:
- Deploy staging from GitHub Actions using a SHA-tagged image.
- Run gateway E2E after deploy:
  - `GET /health`
  - `POST /documents`
  - `GET /documents/{job_id}`
  - `GET /documents/{job_id}/result`
- Run a small benchmark set, maybe 5-10 representative docs.
- Confirm uncertain docs route to human review.
- Confirm webhook events are delivered or skipped with receipts.
- Confirm no unexpected container restarts during the test window.

Evidence:
- GitHub Actions deploy run URL.
- E2E artifacts from `artifacts/e2e/...`
- benchmark artifact summary
- `docker compose ps`
- relevant service logs
- Prometheus/Grafana screenshots or exported metrics

**Backups**
Prove backup and restore, not just backup creation.

Minimum proof:
- Backup Postgres.
- Backup object/artifact storage volumes or buckets.
- Backup `.env.staging` separately in a secure place.
- Restore into a fresh staging-like environment.
- Confirm restored jobs, artifacts, delivery receipts, review tasks, and benchmark artifacts are readable.

Evidence:
- backup timestamp
- backup location
- restore command output
- restored job count
- sample restored artifact checks
- successful post-restore health and result fetch

**Rollback**
Prove you can return to the previous known-good release.

Minimum proof:
- Deploy release candidate A.
- Run smoke/E2E.
- Deploy release candidate B.
- Simulate a failure or mark B bad.
- Roll back to A using the previous image tag.
- Confirm gateway and document flow work again.

Evidence:
- previous image tag
- failed candidate tag
- rollback command or GitHub Action run
- post-rollback `/health`
- post-rollback E2E pass
- compose service image IDs before/after

**Monitoring Alarms**
Prove alerts fire and route to the right humans.

Minimum proof:
- Kill or stop a staging service and confirm Prometheus/Alertmanager fires.
- Trigger high 5xx or failed health checks.
- Fill or simulate low disk/volume pressure if safe.
- Confirm notification delivery through the configured channel.
- Confirm alert resolves after recovery.

Evidence:
- alert name
- time fired
- time acknowledged
- time resolved
- screenshot or exported Alertmanager event
- notification received by email/Slack/etc.
- remediation notes

I’d turn this into the next milestone:

> Staging Operational Readiness: post-deploy smoke tests, backup/restore drill, rollback drill, and alert drill, all with saved artifacts.

After that, production CD can be considered, but still with manual approval.

---

**Validated:**

- drill runner compiles
- drill CLI help works
- GitHub workflow YAML parses
- staging Compose overlay renders with fake image metadata
- restore verification fails clearly when evidence is missing


---


I’d make it **manual-approval production promotion**, not “continuous deployment” in the fully automatic sense.

Suggested next milestone:

**Production Promotion With Manual Approval**

The rule should be:

> Production can only deploy a release candidate that already passed CI, image publishing, staging deploy, staging smoke, backup verification, rollback drill, and alert drill.

I would implement it in this order:

1. **Promotion gate checklist**
   - Require a SHA image tag, not `latest`.
   - Require staging smoke artifact.
   - Require backup/restore evidence.
   - Require rollback drill evidence.
   - Require alert drill evidence.
   - Require human approval through GitHub `production` environment.

2. **Production deploy workflow**
   - Trigger only by `workflow_dispatch`.
   - Inputs:
     - `image_tag`
     - `staging_evidence_url` or artifact/run reference
     - `change_ticket` optional but useful
   - GitHub environment: `production`
   - Use required reviewers in GitHub environment settings.
   - Deploy the same `docker-compose.prod.yml + docker-compose.staging.yml` style image override, but with `.env.production`.

3. **Pre-deploy safety**
   - Run `production_preflight.py` against `.env.production` on the server.
   - Render Compose config.
   - Record current image tags before changing anything.
   - Optionally run a fresh backup command before deploy.

4. **Deploy**
   - Pull SHA-tagged images.
   - Restart only gateway-facing app services first:
     - `ingestion-service`
     - `workflow-orchestrator`
     - `delivery-service`
     - maybe `gateway`
   - Avoid touching stateful services unless intentionally upgrading infra.

5. **Post-deploy verification**
   - Run production smoke test through the public gateway.
   - Require Prometheus scrape targets healthy.
   - Save artifacts.
   - If smoke fails, automatically stop and print rollback command, but I would not auto-rollback yet unless you are very confident.

6. **Rollback workflow**
   - Separate manual workflow:
     - input `previous_image_tag`
     - deploy previous SHA
     - run smoke
     - save rollback evidence

So the milestone should be:

> Manual Production Promotion: approved GitHub workflow deploys a proven release candidate to production, runs preflight and smoke checks, records evidence, and provides a manual rollback workflow.

I would not make production deploy automatic after staging yet. Manual approval is the right level of automation now.


--- 

**Validation passed:**

- GitHub workflow YAML parsed successfully.
- `scripts/staging_operational_drill.py` and `scripts/production_preflight.py` compile.
- `python3 scripts/production_preflight.py --env-file infra/ci/production.env` passed.
- Production Compose with `docker-compose.release.yml` rendered cleanly.

The next practical step is to configure the GitHub `production` environment with required reviewers and the `PRODUCTION_*` secrets before running the workflow.


---

Yes. That’s the right next step before any production run.

Configure this in GitHub:

1. Go to repo `Settings` -> `Environments` -> `New environment`.
2. Name it exactly `production`.
3. Add required reviewers so production deploys pause for manual approval.
4. Add these environment secrets:
   - `PRODUCTION_HOST`
   - `PRODUCTION_USER`
   - `PRODUCTION_SSH_KEY`
   - `PRODUCTION_ENV_FILE`
   - `PRODUCTION_PUBLIC_BASE_URL`
   - `PRODUCTION_SMOKE_API_KEY`
   - `PRODUCTION_PROMETHEUS_URL`
5. Optional:
   - `PRODUCTION_PORT`
   - `PRODUCTION_KNOWN_HOSTS`
6. Add environment variables if needed:
   - `PRODUCTION_APP_DIR`, default `/opt/idp`
   - `PRODUCTION_TENANT_ID`, default `default`

After that, run `Deploy Production` manually with:
- `image_tag`: approved SHA release-candidate tag
- `staging_evidence_url`: link to staging readiness/smoke artifacts
- `run_predeploy_backup`: `true`

Production deploy will still wait for reviewer approval before touching the server.


Assign them like this. Do not use the example values literally.

| GitHub environment secret | Value to put in GitHub |
|---|---|
| `PRODUCTION_HOST` | Production server DNS name or IP, for example `idp.example.com` or `203.0.113.10`. |
| `PRODUCTION_USER` | SSH user on that server, for example `deploy` or `ubuntu`. It must be able to run Docker Compose. |
| `PRODUCTION_SSH_KEY` | Private SSH key for `PRODUCTION_USER`, including `-----BEGIN ...-----` and `-----END ...-----`. Prefer a deploy-only key. |
| `PRODUCTION_ENV_FILE` | The full contents of your real `.env.production` file, after replacing every placeholder and passing `python3 scripts/production_preflight.py --env-file .env.production`. |
| `PRODUCTION_PUBLIC_BASE_URL` | Public gateway URL clients use, for example `https://idp.example.com`. This should match `PUBLIC_BASE_URL` in `.env.production`. |
| `PRODUCTION_SMOKE_API_KEY` | One valid API key from `INGESTION_API_KEYS` in `.env.production`, without the tenant suffix. Example: if `INGESTION_API_KEYS=abc123:default`, this secret is `abc123`. |
| `PRODUCTION_PROMETHEUS_URL` | URL the GitHub runner can reach for Prometheus checks. If Prometheus is private on the production server, use a VPN/private URL or a secure reverse-proxy URL, for example `https://prometheus.idp.example.com`. |

Optional:

| GitHub environment secret/variable | Value |
|---|---|
| `PRODUCTION_PORT` | SSH port. Use `22` unless the server uses another SSH port. |
| `PRODUCTION_KNOWN_HOSTS` | Output of `ssh-keyscan -p <port> <host>` after you verify the host fingerprint. Better than letting CI trust first use. |
| `PRODUCTION_APP_DIR` | GitHub environment variable, not secret. Remote app directory, default `/opt/idp`. |
| `PRODUCTION_TENANT_ID` | GitHub environment variable, not secret. Tenant for smoke tests, usually `default` unless your smoke key maps to another tenant. |

The most important one is `PRODUCTION_ENV_FILE`: it should be a complete production env file with real values for Postgres, S3/SeaweedFS, ingestion API keys, Label Studio, Grafana, Alertmanager SMTP, webhook secrets, and public URLs. Then `PRODUCTION_SMOKE_API_KEY` must match one of those configured ingestion keys.

--- 

### GitHub environment secret (Production Configuration)

Configure the `production` environment in GitHub with the following secrets and values.
Do not commit real secret material to this file.

```bash
PRODUCTION_HOST=idpocr.com

PRODUCTION_USER=deployer

PRODUCTION_SSH_KEY=-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACC/gzhDYEvk4qi/yndJnY7izIMLv3OaYOeVk/imnNAIcQAAAJhDpDLCQ6Qy
wgAAAAtzc2gtZWQyNTUxOQAAACC/gzhDYEvk4qi/yndJnY7izIMLv3OaYOeVk/imnNAIcQ
AAAEBUsbxSOJXsJb9f43oCzcUqaUFY1vmmQ6LRGgiGh42Bn7+DOENgS+TiqL/Kd0mdjuLM
gwu/c5pg55WT+Kac0AhxAAAAE2RlcGxveWVyQGlkcG9jci5jb20BAg==
-----END OPENSSH PRIVATE KEY-----

PRODUCTION_ENV_FILE=<paste the full multiline .env.production contents>

PRODUCTION_PUBLIC_BASE_URL=https://idpocr.com

PRODUCTION_SMOKE_API_KEY=zuQAxoLS6SSGmtsF5shSJ1iTy6xAc7qndJhV50YXp4vmDFza

PRODUCTION_PROMETHEUS_URL=https://prometheus.idpocr.com

PRODUCTION_PORT=22

PRODUCTION_KNOWN_HOSTS=<verified output of ssh-keyscan -p 22 idpocr.com>

PRODUCTION_APP_DIR=/opt/idp

PRODUCTION_TENANT_ID=default



```

```bash
ssh-keyscan -p 22 idpocr.com
```

```bash
cat ./id_ed25519_production
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACC/gzhDYEvk4qi/yndJnY7izIMLv3OaYOeVk/imnNAIcQAAAJhDpDLCQ6Qy
wgAAAAtzc2gtZWQyNTUxOQAAACC/gzhDYEvk4qi/yndJnY7izIMLv3OaYOeVk/imnNAIcQ
AAAEBUsbxSOJXsJb9f43oCzcUqaUFY1vmmQ6LRGgiGh42Bn7+DOENgS+TiqL/Kd0mdjuLM
gwu/c5pg55WT+Kac0AhxAAAAE2RlcGxveWVyQGlkcG9jci5jb20BAg==
-----END OPENSSH PRIVATE KEY-----
```
---

### Github environment secret (Staging Configuration)

Get SSH key for staging deploys:
```bash
ssh-keygen -t ed25519 -C "deployer@staging-idp.example.com" -f ./id_ed25519_staging
```

```bash
cat ./id_ed25519_staging
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACC+8xYC1uU0rAfAGNcwuwICw2F15Nfjyqf0kRaCOdAD/QAAAKg7qLjLO6i4
ywAAAAtzc2gtZWQyNTUxOQAAACC+8xYC1uU0rAfAGNcwuwICw2F15Nfjyqf0kRaCOdAD/Q
AAAECP7X72rCtJYL3tsGzup1j8GBnAA8CF7F2udz05iqC2br7zFgLW5TSsB8AY1zC7AgLD
YXXk1+PKp/SRFoI50AP9AAAAIGRlcGxveWVyQHN0YWdpbmctaWRwLmV4YW1wbGUuY29tAQ
IDBAU=
-----END OPENSSH PRIVATE KEY-----
```


```bash
STAGING_HOST: staging-idp.example.com
STAGING_USER: deployer

STAGING_SSH_KEY:-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACC+8xYC1uU0rAfAGNcwuwICw2F15Nfjyqf0kRaCOdAD/QAAAKg7qLjLO6i4
ywAAAAtzc2gtZWQyNTUxOQAAACC+8xYC1uU0rAfAGNcwuwICw2F15Nfjyqf0kRaCOdAD/Q
AAAECP7X72rCtJYL3tsGzup1j8GBnAA8CF7F2udz05iqC2br7zFgLW5TSsB8AY1zC7AgLD
YXXk1+PKp/SRFoI50AP9AAAAIGRlcGxveWVyQHN0YWdpbmctaWRwLmV4YW1wbGUuY29tAQ
IDBAU=
-----END OPENSSH PRIVATE KEY-----

STAGING_ENV_FILE: <paste the full multiline .env.staging contents>

# for local setting
STAGING_PUBLIC_BASE_URL=http://127.0.0.1:8081
STAGING_PROMETHEUS_URL=

# STAGING_PUBLIC_BASE_URL: https://staging-idp.example.com
STAGING_SMOKE_API_KEY: bd28f24724ffbbb1ed0cab76f93d8eee2e044d15d8ddac4f
# STAGING_PROMETHEUS_URL: https://staging-prometheus.example.com
STAGING_PORT: 22
STAGING_TENANT_ID: default
STAGING_KNOWN_HOSTS: staging-idp.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII3xWAkA77qPcfCl2UQBBXlztzSpLWbxtEty/s4qpdwz



STAGING_USE_CLOUDFLARE=true
STAGING_APP_DIR=/opt/idp-staging
PUBLIC_BASE_URL=https://staging-idp.example.com
IDP_ENVIRONMENT=staging


```

Split Tunnels: Ensure the 100.64.0.0/10 range and your private origin IPs are not part of an Exclude rule.
Local Domain Fallback: Ensure this hostname is not included in a local domain fallback list.

### Cloudfare 

installation:
```bash
brew install cloudflared && 

sudo cloudflared service install eyJhIjoiMWNjOThiNTU2ZmNkZWFhYTczOTE4NDVkZGY2ZjQ2M2YiLCJ0IjoiNTIxZTk0ZjQtZWEzMy00NTc4LTljYmItNDFmNTliODE4NjQyIiwicyI6Ik5HSTFabUprWm1RdE5tUTNNQzAwWTJFM0xXSmxOMll0WVRBMk1XVm1OR1psWlRGbCJ9
```

Tunnel name: staging-server
password: sillians2536

