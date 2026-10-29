"""Create Northflank project and combined service for Moataz Media Bot.

All credentials are supplied by runtime environment, never committed to Git.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://api.northflank.com/v1"
PROJECT_NAME = "Moataz Media Bot"
SERVICE_NAME = "Moataz Media Bot"
REPOSITORY = "https://github.com/Mtzallqmy/Moataz-a"

DEFAULT_ENV = {
    "APP_NAME": "Moataz Media Bot",
    "DEFAULT_LANGUAGE": "ar",
    "PORT": "8080",
    "DOWNLOAD_DIR": "/data/downloads",
    "PROJECT_DIR": "/data/projects",
    "RENDER_TEMP_DIR": "/data/tmp",
    "MAX_CONCURRENT_JOBS": "2",
    "MAX_JOBS_PER_USER": "1",
    "MAX_BULK_URLS": "10",
    "MAX_PLAYLIST_ITEMS": "10",
    "MAX_VIDEO_DURATION_SECONDS": "14400",
    "MAX_FILE_SIZE_MB": "512",
    "TELEGRAM_UPLOAD_LIMIT_MB": "49",
    "PROGRESS_UPDATE_SECONDS": "2.5",
    "TEMP_RETENTION_SECONDS": "21600",
    "PROBE_TIMEOUT_SECONDS": "120",
    "DOWNLOAD_TIMEOUT_SECONDS": "900",
    "YTDLP_SOCKET_TIMEOUT_SECONDS": "30",
    "YTDLP_RETRIES": "2",
    "YTDLP_FRAGMENT_RETRIES": "3",
    "YTDLP_CONCURRENT_FRAGMENTS": "2",
    "YTDLP_IMPERSONATE": "true",
    "PYTUBEFIX_ENABLED": "true",
    "GALLERYDL_ENABLED": "true",
    "GALLERYDL_TIMEOUT_SECONDS": "180",
    "INSTALOADER_ENABLED": "true",
    "TIKTOK_BACKEND_ENABLED": "false",
    "COBALT_API_URLS": "",
    "COBALT_API_TOKEN": "",
    "COBALT_AUTH_SCHEME": "Api-Key",
    "COBALT_TIMEOUT_SECONDS": "45",
    "DOWNLOAD_BACKEND_FAILURE_THRESHOLD": "3",
    "DOWNLOAD_BACKEND_COOLDOWN_SECONDS": "120",
    "JOB_MAX_RETRIES": "2",
    "JOB_RETRY_BASE_SECONDS": "4",
    "JOB_RETRY_CAP_SECONDS": "45",
    "FFMPEG_TIMEOUT_SECONDS": "900",
    "FFMPEG_KILL_GRACE_SECONDS": "3",
    "MAX_PROJECT_ASSETS": "20",
    "MAX_PROJECT_DURATION_SECONDS": "1800",
    "MAX_RENDER_DURATION_SECONDS": "1800",
    "MAX_CONCURRENT_RENDERS": "1",
    "MAX_RENDERS_PER_USER": "1",
    "RENDER_TIMEOUT_SECONDS": "1800",
    "MAX_RENDER_RETRIES": "1",
    "DEFAULT_RENDER_FPS": "30",
    "OPENAI_BASE_URL": "",
    "OPENAI_API_TOKEN": "",
    "DASHBOARD_USERNAME": "admin",
}
REQUIRED = ("NORTHFLANK_API_TOKEN", "BOT_TOKEN", "DATABASE_URL", "DASHBOARD_PASSWORD")


def runtime_environment() -> dict[str, str]:
    result = {key: os.environ.get(key, default) for key, default in DEFAULT_ENV.items()}
    result.update({key: os.environ[key] for key in ("BOT_TOKEN", "DATABASE_URL", "DASHBOARD_PASSWORD")})
    return result


def service_spec() -> dict:
    return {
        "name": SERVICE_NAME,
        "description": "Telegram polling, download manager and web dashboard",
        "billing": {"deploymentPlan": os.environ.get("NORTHFLANK_DEPLOYMENT_PLAN") or "nf-compute-20"},
        "deployment": {
            "instances": 1,
            "docker": {"configType": "default"},
            "storage": {"ephemeralStorage": {"storageSize": 2048}},
        },
        "ports": [
            {"name": "web", "internalPort": 8080, "public": True, "protocol": "HTTP"}
        ],
        "disabledCI": False,
        "buildSource": "git",
        "vcsData": {
            "projectUrl": REPOSITORY,
            "projectType": "github",
            "projectBranch": "main",
        },
        "buildSettings": {
            "dockerfile": {
                "buildEngine": "buildkit",
                "dockerFilePath": "/Dockerfile",
                "dockerWorkDir": "/",
            }
        },
        "runtimeEnvironment": runtime_environment(),
        "healthChecks": [
            {
                "protocol": "HTTP",
                "type": "readinessProbe",
                "path": "/readyz",
                "port": 8080,
                "initialDelaySeconds": 40,
                "periodSeconds": 30,
                "timeoutSeconds": 10,
                "failureThreshold": 5,
                "successThreshold": 1,
            }
        ],
    }


class NorthflankError(RuntimeError):
    pass


def api(method: str, route: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        BASE_URL + route,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["NORTHFLANK_API_TOKEN"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=35) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Never output API error bodies; they might echo supplied secrets.
        raise NorthflankError(f"Northflank API {method} {route} returned HTTP {exc.code}") from None
    except (URLError, TimeoutError) as exc:
        raise NorthflankError(f"Unable to reach Northflank API ({type(exc).__name__})") from None


def entries(route: str, field: str) -> list[dict]:
    items: list[dict] = []
    cursor = ""
    for _ in range(30):
        page_route = route + ("?cursor=" + quote(cursor) if cursor else "?per_page=100")
        result = api("GET", page_route)
        items.extend(result["data"][field])
        pagination = result.get("pagination") or {}
        if not pagination.get("hasNextPage"):
            return items
        cursor = pagination.get("cursor") or ""
        if not cursor:
            raise NorthflankError(f"Missing cursor while listing {field}")
    raise NorthflankError(f"Too many pages while listing {field}")


def run() -> int:
    missing = [key for key in REQUIRED if not os.environ.get(key)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        return 2
    team_id = os.environ.get("NORTHFLANK_TEAM_ID", "").strip()
    prefix = "/teams/" + quote(team_id, safe="") if team_id else ""
    project_route = prefix + "/projects"
    project_id = os.environ.get("NORTHFLANK_PROJECT_ID", "").strip()
    if not project_id:
        existing = next((p for p in entries(project_route, "projects") if p.get("name") == PROJECT_NAME), None)
        if existing:
            project_id = existing["id"]
            print(f"Using existing Northflank project: {project_id}")
        else:
            result = api(
                "POST", project_route,
                {"name": PROJECT_NAME, "description": "Moataz Media Bot deployment",
                 "region": "europe-west", "color": "#267E81"},
            )
            project_id = result["data"]["id"]
            print(f"Created Northflank project: {project_id}")
    services_route = project_route + "/" + quote(project_id, safe="") + "/services"
    existing_services = entries(services_route, "services")
    existing = next((s for s in existing_services if s.get("name") == SERVICE_NAME), None)
    if existing:
        print(f"Service already exists: {existing['id']} (no changes made)")
        return 0
    result = api("POST", services_route + "/combined", service_spec())
    service_id = result["data"]["id"]
    print(f"Created combined service: {service_id}")
    print("Build requested. Verify /readyz, container logs and Telegram polling in Northflank.")
    print("Note: /data is ephemeral until a persistent volume is attached.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (NorthflankError, KeyError, ValueError) as exc:
        print(f"Deployment request failed: {exc}", file=sys.stderr)
        sys.exit(1)
