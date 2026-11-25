"""Public GitHub release metadata and selectable downloadable assets."""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import aiohttp

from app.errors import InvalidURLError
from app.security import normalize_media_url
from app.services.artifact_urls import artifact_target

_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_ASSETS = 500
_CACHE_TTL = 180


@dataclass(frozen=True, slots=True)
class ReleaseReference:
    owner: str
    repo: str
    tag: str | None = None
    repository_root: bool = False
    prefer_recent: bool = False

    @property
    def page_url(self) -> str:
        suffix = f"/tag/{quote(self.tag, safe='')}" if self.tag else "" if self.repository_root or self.prefer_recent else "/latest"
        return f"https://github.com/{self.owner}/{self.repo}/releases{suffix}"


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int | None


@dataclass(frozen=True, slots=True)
class ReleaseCatalog:
    repository: str
    tag: str
    title: str
    page_url: str
    assets: tuple[ReleaseAsset, ...]
    truncated: bool = False


class ReleaseUnavailable(RuntimeError):
    pass


def release_reference(url: str) -> ReleaseReference | None:
    try:
        parsed = urlsplit(normalize_media_url(url))
    except InvalidURLError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or parsed.port not in {None, 443} or parsed.query:
        return None
    segments = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(segments) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in {".", ".."} for p in segments[:2]):
        return None
    owner, repo = segments[:2]
    repo = repo.removesuffix(".git")
    if not repo:
        return None
    if len(segments) == 2:
        return ReleaseReference(owner, repo, repository_root=True)
    if segments[2:] == ["releases"]:
        return ReleaseReference(owner, repo, prefer_recent=True)
    if segments[2:] == ["releases", "latest"]:
        return ReleaseReference(owner, repo)
    if len(segments) >= 5 and segments[2:4] == ["releases", "tag"]:
        tag = "/".join(segments[4:])
        if tag and len(tag) <= 200 and all(part not in {"", ".", ".."} for part in tag.split("/")) and not any(c in tag for c in "\\\x00\r\n"):
            return ReleaseReference(owner, repo, tag=tag)
    return None


async def _github_json(session: aiohttp.ClientSession, path: str, *, params: dict | None = None):
    url = "https://api.github.com" + path
    async with session.get(url, params=params, allow_redirects=False) as response:
        if response.status == 404:
            raise ReleaseUnavailable("No public GitHub release was found")
        if response.status in {403, 429}:
            raise RuntimeError("HTTP 429: GitHub release API limit or access denied")
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: GitHub release API unavailable")
        if response.content_length is not None and response.content_length > _MAX_RESPONSE:
            raise RuntimeError("GitHub release metadata too large")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > _MAX_RESPONSE:
                raise RuntimeError("GitHub release metadata too large")
        return json.loads(data)


def _catalog(ref: ReleaseReference, release: dict, raw_assets: list, truncated: bool = False) -> ReleaseCatalog:
    tag = str(release.get("tag_name") or "")
    if not tag or len(tag) > 200 or any(p in {"", ".", ".."} for p in tag.split("/")) or any(c in tag for c in "\\\x00\r\n"):
        raise ReleaseUnavailable("GitHub release has an invalid tag")
    repo = f"{ref.owner}/{ref.repo}"
    prefix = f"https://github.com/{repo}"
    assets: list[ReleaseAsset] = []
    seen: set[str] = set()
    for item in raw_assets[:_MAX_ASSETS]:
        if not isinstance(item, dict) or item.get("state") not in {None, "uploaded"}:
            continue
        url = str(item.get("browser_download_url") or "")
        try:
            target = artifact_target(url)
        except InvalidURLError:
            continue
        if not target.url.lower().startswith(f"{prefix}/releases/download/".lower()) or target.url in seen:
            continue
        seen.add(target.url)
        size = item.get("size")
        assets.append(ReleaseAsset(str(item.get("name") or target.filename)[:250], target.url, size if isinstance(size, int) and size >= 0 else None))
    # Source archives are available even when no binary files were attached.
    encoded_tag = "/".join(quote(part, safe="-._~") for part in tag.split("/"))
    assets.extend((ReleaseAsset("Source code (ZIP)", f"{prefix}/archive/refs/tags/{encoded_tag}.zip", None),
                   ReleaseAsset("Source code (TAR.GZ)", f"{prefix}/archive/refs/tags/{encoded_tag}.tar.gz", None)))
    if ref.repository_root:
        assets.append(ReleaseAsset("Repository (HEAD ZIP)", f"{prefix}/archive/HEAD.zip", None))
    return ReleaseCatalog(repo, tag, str(release.get("name") or tag)[:200], f"{prefix}/releases/tag/{quote(tag, safe='')}", tuple(assets), truncated)


_cache: OrderedDict[ReleaseReference, tuple[float, ReleaseCatalog]] = OrderedDict()


async def fetch_release_catalog(ref: ReleaseReference) -> ReleaseCatalog:
    now = time.monotonic()
    cached = _cache.get(ref)
    if cached and cached[0] > now:
        _cache.move_to_end(ref)
        return cached[1]
    prefix = f"/repos/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20, sock_read=10), trust_env=False, headers={"Accept": "application/vnd.github+json", "User-Agent": "MoatazMediaBot"}) as session:
        async def recent_release():
            # The release listing includes RC/beta releases which /latest may omit.
            listing = await _github_json(session, f"{prefix}/releases", params={"per_page": 1})
            if not isinstance(listing, list) or not listing:
                raise ReleaseUnavailable("No public GitHub release was found")
            return listing[0]

        if ref.tag:
            release = await _github_json(session, f"{prefix}/releases/tags/{quote(ref.tag, safe='')}")
        elif ref.repository_root or ref.prefer_recent:
            release = await recent_release()
        else:
            try:
                release = await _github_json(session, f"{prefix}/releases/latest")
            except ReleaseUnavailable:
                release = await recent_release()
        if not isinstance(release, dict) or release.get("draft"):
            raise ReleaseUnavailable("No public GitHub release was found")
        raw_assets = release.get("assets") or []
        if not isinstance(raw_assets, list):
            raise ReleaseUnavailable("GitHub release data is unavailable")
        truncated = False
        release_id = release.get("id")
        if len(raw_assets) >= 100 and isinstance(release_id, int):
            raw_assets = []
            for page in range(1, 6):
                items = await _github_json(session, f"{prefix}/releases/{release_id}/assets", params={"per_page": 100, "page": page})
                if not isinstance(items, list):
                    raise ReleaseUnavailable("GitHub asset data is unavailable")
                raw_assets.extend(items)
                if len(items) < 100:
                    break
            else:
                truncated = True
        catalog = _catalog(ref, release, raw_assets, truncated)
    _cache[ref] = (time.monotonic() + _CACHE_TTL, catalog)
    _cache.move_to_end(ref)
    while len(_cache) > 128:
        _cache.popitem(last=False)
    return catalog
