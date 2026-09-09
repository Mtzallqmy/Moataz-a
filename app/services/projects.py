from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import MediaAsset, MediaProject, ProjectAsset, ProjectStatus, SessionLocal

PRESETS: dict[str, tuple[str, int, int]] = {
    "vertical": ("9:16", 1080, 1920),
    "horizontal": ("16:9", 1920, 1080),
    "square": ("1:1", 1080, 1080),
}
TEMPLATES = {"auto", "audio_image", "slideshow", "merge_videos", "video_audio", "intro_main_outro", "logo_overlay"}
ROLES = {"main", "intro", "outro", "music", "voice", "logo", "background"}


@dataclass(frozen=True, slots=True)
class ProjectAssetItem:
    link: ProjectAsset
    asset: MediaAsset


def _timeline_config(project: MediaProject) -> dict[str, Any]:
    try:
        payload = json.loads(project.timeline_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("version", 1)
    payload.setdefault("template", "auto")
    payload.setdefault("options", {})
    return payload


class ProjectService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def create_project(self, *, user_id: int, chat_id: int, name: str | None = None, preset: str = "vertical") -> MediaProject:
        if preset not in PRESETS:
            raise ValueError("Unknown render preset")
        aspect_ratio, width, height = PRESETS[preset]
        timeline = {
            "version": 1,
            "canvas": {"width": width, "height": height, "fps": self.settings.default_render_fps, "background": "black"},
            "template": "auto",
            "options": {"fit_mode": "fit", "transition": "none"},
            "tracks": [],
        }
        async with SessionLocal() as session:
            project = MediaProject(
                user_id=user_id,
                chat_id=chat_id,
                name=(name or "Media Project")[:255],
                status=ProjectStatus.DRAFT.value,
                aspect_ratio=aspect_ratio,
                width=width,
                height=height,
                fps=self.settings.default_render_fps,
                timeline_json=json.dumps(timeline, separators=(",", ":")),
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)
            session.expunge(project)
            return project

    async def get_project(self, project_id: int, *, user_id: int | None = None) -> MediaProject | None:
        async with SessionLocal() as session:
            statement = select(MediaProject).where(MediaProject.id == project_id)
            if user_id is not None:
                statement = statement.where(MediaProject.user_id == user_id)
            project = await session.scalar(statement)
            if project is not None:
                session.expunge(project)
            return project

    async def list_projects(self, user_id: int, *, limit: int = 10) -> list[MediaProject]:
        async with SessionLocal() as session:
            projects = list(await session.scalars(
                select(MediaProject).where(MediaProject.user_id == user_id).order_by(MediaProject.id.desc()).limit(max(1, min(int(limit), 50)))
            ))
            for project in projects:
                session.expunge(project)
            return projects

    async def list_assets(self, project_id: int, *, user_id: int | None = None) -> list[ProjectAssetItem]:
        async with SessionLocal() as session:
            statement = (
                select(ProjectAsset, MediaAsset)
                .join(MediaAsset, MediaAsset.id == ProjectAsset.asset_id)
                .join(MediaProject, MediaProject.id == ProjectAsset.project_id)
                .where(ProjectAsset.project_id == project_id)
                .order_by(ProjectAsset.position, ProjectAsset.asset_id)
            )
            if user_id is not None:
                statement = statement.where(MediaProject.user_id == user_id)
            rows = (await session.execute(statement)).all()
            result: list[ProjectAssetItem] = []
            for link, asset in rows:
                session.expunge(link)
                session.expunge(asset)
                result.append(ProjectAssetItem(link=link, asset=asset))
            return result

    async def _write_timeline(self, session, project: MediaProject) -> None:
        rows = (await session.execute(
            select(ProjectAsset, MediaAsset)
            .join(MediaAsset, MediaAsset.id == ProjectAsset.asset_id)
            .where(ProjectAsset.project_id == project.id)
            .order_by(ProjectAsset.position, ProjectAsset.asset_id)
        )).all()
        payload = _timeline_config(project)
        payload["canvas"] = {"width": project.width, "height": project.height, "fps": project.fps, "background": "black"}
        payload["tracks"] = [{"type": "ordered", "clips": [
            {"asset_id": asset.id, "asset_type": asset.asset_type, "position": link.position, "role": link.role}
            for link, asset in rows
        ]}] if rows else []
        project.timeline_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        project.status = ProjectStatus.READY.value if rows else ProjectStatus.DRAFT.value

    async def add_asset(self, project_id: int, asset_id: int, *, user_id: int, role: str = "main") -> ProjectAsset:
        if role not in ROLES:
            raise ValueError("Unknown asset role")
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            asset = await session.scalar(select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.user_id == user_id))
            if project is None or asset is None:
                raise LookupError("Project or asset not found")
            existing = await session.get(ProjectAsset, (project_id, asset_id))
            if existing is not None:
                session.expunge(existing)
                return existing
            count = int(await session.scalar(select(func.count()).select_from(ProjectAsset).where(ProjectAsset.project_id == project_id)) or 0)
            if count >= self.settings.max_project_assets:
                raise ValueError("Project asset limit reached")
            max_position = await session.scalar(select(func.max(ProjectAsset.position)).where(ProjectAsset.project_id == project_id))
            link = ProjectAsset(project_id=project_id, asset_id=asset_id, position=(int(max_position) + 1) if max_position is not None else 0, role=role)
            session.add(link)
            await session.flush()
            await self._write_timeline(session, project)
            await session.commit()
            await session.refresh(link)
            session.expunge(link)
            return link

    async def remove_asset(self, project_id: int, asset_id: int, *, user_id: int) -> bool:
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            if project is None:
                return False
            link = await session.get(ProjectAsset, (project_id, asset_id))
            if link is None:
                return False
            await session.delete(link)
            await session.flush()
            links = list(await session.scalars(select(ProjectAsset).where(ProjectAsset.project_id == project_id).order_by(ProjectAsset.position, ProjectAsset.asset_id)))
            for index, item in enumerate(links):
                item.position = index
            await self._write_timeline(session, project)
            await session.commit()
            return True

    async def move_asset(self, project_id: int, asset_id: int, *, user_id: int, direction: int) -> bool:
        step = -1 if direction < 0 else 1
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            if project is None:
                return False
            links = list(await session.scalars(select(ProjectAsset).where(ProjectAsset.project_id == project_id).order_by(ProjectAsset.position, ProjectAsset.asset_id)))
            index = next((idx for idx, item in enumerate(links) if item.asset_id == asset_id), None)
            if index is None:
                return False
            target = index + step
            if target < 0 or target >= len(links):
                return False
            links[index].position, links[target].position = links[target].position, links[index].position
            await session.flush()
            await self._write_timeline(session, project)
            await session.commit()
            return True

    async def set_role(self, project_id: int, asset_id: int, *, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise ValueError("Unknown asset role")
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            link = await session.get(ProjectAsset, (project_id, asset_id))
            if project is None or link is None:
                raise LookupError("Project asset not found")
            link.role = role
            await self._write_timeline(session, project)
            await session.commit()

    async def set_preset(self, project_id: int, *, user_id: int, preset: str) -> MediaProject:
        if preset not in PRESETS:
            raise ValueError("Unknown render preset")
        aspect_ratio, width, height = PRESETS[preset]
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            if project is None:
                raise LookupError("Project not found")
            project.aspect_ratio = aspect_ratio
            project.width = width
            project.height = height
            project.fps = self.settings.default_render_fps
            await self._write_timeline(session, project)
            await session.commit()
            await session.refresh(project)
            session.expunge(project)
            return project

    async def apply_template(self, project_id: int, *, user_id: int, template: str, options: dict[str, Any] | None = None) -> MediaProject:
        if template not in TEMPLATES:
            raise ValueError("Unknown studio template")
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            if project is None:
                raise LookupError("Project not found")
            payload = _timeline_config(project)
            payload["template"] = template
            merged = dict(payload.get("options") or {})
            merged.update(options or {})
            if merged.get("fit_mode", "fit") not in {"fit", "fill", "blur-background"}:
                raise ValueError("Unknown fit mode")
            if merged.get("transition", "none") not in {"none", "fade"}:
                raise ValueError("Unknown transition")
            payload["options"] = merged
            project.timeline_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            await self._write_timeline(session, project)
            await session.commit()
            await session.refresh(project)
            session.expunge(project)
            return project

    async def cancel_project(self, project_id: int, *, user_id: int) -> bool:
        async with SessionLocal() as session:
            project = await session.scalar(select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id))
            if project is None:
                return False
            project.status = ProjectStatus.CANCELLED.value
            await session.commit()
            return True


project_service = ProjectService()
