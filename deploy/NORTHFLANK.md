# Northflank deployment (Moataz Media Bot)

This repository contains the Dockerfile for deployment. The bot uses Telegram **polling**, so **exactly one** running replica is necessary.

## Deploy from GitHub Actions (also works from a phone)

1. On Northflank, authorize GitHub access to `Mtzallqmy/Moataz-a` if it is not already connected. Choose the **Sandbox** plan for free-tier testing.
2. In this GitHub repository, open **Settings > Secrets and variables > Actions**. Add these **Repository secrets** individually, preferably with freshly rotated credentials:
   - `NORTHFLANK_API_TOKEN`: team API token with project/service create and read permissions.
   - `BOT_TOKEN`: Telegram bot token.
   - `DATABASE_URL`: existing Neon PostgreSQL connection URL; no new database is created.
   - `DASHBOARD_PASSWORD`: a strong dashboard password.
3. Select **Actions > Deploy to Northflank > Run workflow** on the `main` branch.
4. In Northflank, inspect build and deployment logs, verify `/readyz`, confirm Telegram polling and test an authorized media download.

Optional GitHub repository variables: `NORTHFLANK_PROJECT_ID` to target an existing project; `NORTHFLANK_DEPLOYMENT_PLAN` (defaults to `nf-compute-20`).

## Deployment configuration

- Project and service: `Moataz Media Bot`
- New project region: `europe-west`
- Combined service from `https://github.com/Mtzallqmy/Moataz-a`, branch `main`
- Dockerfile at `/Dockerfile`, which starts `python -m app.main`
- One instance, HTTP port `8080`, readiness `/readyz`
- The provisioning script applies all non-secret values from the supplied configuration and receives application secrets via GitHub Actions.
- Existing same-named services are **not overwritten**. A successful creation request is **not** proof of completed deployment.

## Storage and resource limits

- `/data/downloads`, `/data/projects`, `/data/tmp` are **ephemeral** until a persistent volume is mounted at `/data`. Reboots/redeployments may remove files and incomplete projects.
- The default `nf-compute-20` plan is a low-resource sandbox starting point (512 MiB RAM). FFmpeg processing and large videos may require fewer concurrent jobs or a larger paid resource plan.
- The Sandbox free tier has quotas and is not appropriate for a guaranteed production SLA.
- Never commit API keys, cookies, passwords, or `.env` files.

## Links

- [Northflank combined service API](https://northflank.com/docs/v1/api/project/services/create-combined-service)
- [Northflank pricing](https://northflank.com/pricing)
