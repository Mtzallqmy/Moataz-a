# Production deployment

Repository: `Mtzallqmy/Moataz-a`.

The dedicated Neon project `moataz-media-bot` (`late-thunder-87215465`, AWS Frankfurt) contains the initial 13-table schema from `neon_schema.sql`. Database: `moataz_media`. No previous service data has been migrated; the old Railway project is not accessible through the current connection.

Before switching an existing service to this database, back up and migrate its existing records. Do not overwrite another app’s database or credentials.

Railway configuration:

- Source: this repository, `main`; Dockerfile at repository root.
- Start: `python -m app.main`; one replica for Telegram polling.
- Healthcheck: `/readyz`; persistent volume mounted at `/data`.
- Secrets: `BOT_TOKEN` for this downloader and `DATABASE_URL` from the dedicated Neon project.
- Non-secret defaults: `DOWNLOAD_DIR=/data/downloads`, `PROJECT_DIR=/data/projects`, `RENDER_TEMP_DIR=/data/tmp`, `PROBE_TIMEOUT_SECONDS=120`, `DOWNLOAD_TIMEOUT_SECONDS=900`.
- Optional backends require actual authorized endpoints/cookies. No public relay is silently configured.

Verify `/readyz`, Telegram polling logs, a real download, and the completed file’s local cut/split actions separately. A successful healthcheck alone does not verify YouTube downloads.
