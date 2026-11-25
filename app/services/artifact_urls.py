"""Normalize public software download URLs without accepting arbitrary redirects."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from app.errors import InvalidURLError
from app.security import normalize_media_url

HOSTS = {
    "github.com": "github", "www.github.com": "github",
    "raw.githubusercontent.com": "github", "codeload.github.com": "github",
    "gitlab.com": "gitlab", "www.gitlab.com": "gitlab",
    "huggingface.co": "huggingface", "www.huggingface.co": "huggingface",
    "files.pythonhosted.org": "pypi",
}
ARTIFACT_PLATFORMS = frozenset(HOSTS.values())


@dataclass(frozen=True, slots=True)
class ArtifactTarget:
    platform: str
    url: str
    filename: str


def artifact_platform(url: str) -> str | None:
    return HOSTS.get((urlsplit(url).hostname or "").lower())


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.() +@-]", "_", unquote(name)).strip(". ")
    return name[:200] or "download.bin"


def artifact_target(url: str) -> ArtifactTarget:
    parsed = urlsplit(normalize_media_url(url))
    host = parsed.hostname or ""
    platform = HOSTS.get(host)
    if not platform or parsed.scheme != "https" or parsed.port not in {None, 443} or any((k, v.lower()) not in {("raw", "true"), ("download", "true")} for k, v in parse_qsl(parsed.query, keep_blank_values=True)):
        raise InvalidURLError("Use a public HTTPS software download URL without credentials or parameters")
    parts = parsed.path.strip("/").split("/")
    decoded = [unquote(part) for part in parts]
    # Only a release tag may contain an encoded slash. Keep file-name and owner
    # segments structural, and reject empty/traversal/control segments.
    github_tag = host in {"github.com", "www.github.com"} and len(decoded) >= 6 and decoded[2:4] == ["releases", "download"]
    for index, part in enumerate(decoded):
        values = part.split("/") if github_tag and index == 4 else [part]
        if any(not value or value in {".", ".."} or any(c in value for c in "/\\\x00\r\n") for value in values):
            raise InvalidURLError("Invalid software download path")
    path = "/".join(quote(p, safe="-._~@+") for p in decoded)
    if host in {"github.com", "www.github.com"}:
        if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in decoded[:2]):
            raise InvalidURLError("Use a GitHub repository or public release asset URL")
        owner, repo = decoded[:2]
        repo = repo.removesuffix(".git")
        if not repo:
            raise InvalidURLError("Invalid GitHub repository")
        prefix = f"https://github.com/{owner}/{repo}"
        if len(parts) == 2:
            return ArtifactTarget(platform, f"{prefix}/archive/HEAD.zip", f"{repo}.zip")
        if len(parts) >= 4 and decoded[2] == "tree":
            ref = "/".join(quote(p, safe="-._~") for p in decoded[3:])
            return ArtifactTarget(platform, f"{prefix}/archive/{ref}.zip", f"{repo}-{safe_filename(ref)}.zip")
        if len(parts) >= 5 and decoded[2] in {"blob", "raw"}:
            raw_path = "/".join(quote(p, safe="-._~@+") for p in decoded[:2] + decoded[3:])
            return ArtifactTarget(platform, f"https://raw.githubusercontent.com/{raw_path}", safe_filename(parts[-1]))
        if len(parts) >= 6 and decoded[2:4] == ["releases", "download"]:
            return ArtifactTarget(platform, f"https://github.com/{path}", safe_filename(parts[-1]))
        if len(parts) == 6 and decoded[2:5] == ["releases", "latest", "download"]:
            return ArtifactTarget(platform, f"https://github.com/{path}", safe_filename(parts[-1]))
        if len(parts) >= 4 and decoded[2] == "archive" and decoded[-1].endswith((".zip", ".tar.gz")):
            return ArtifactTarget(platform, f"https://github.com/{path}", f"{repo}-{safe_filename(parts[-1])}")
        if len(parts) >= 5 and decoded[2:4] == ["releases", "tag"]:
            ref = "/".join(quote(p, safe="-._~") for p in decoded[4:])
            return ArtifactTarget(platform, f"{prefix}/archive/refs/tags/{ref}.zip", f"{repo}-{safe_filename(ref)}.zip")
    elif host == "raw.githubusercontent.com" and len(parts) >= 4:
        return ArtifactTarget(platform, f"https://{host}/{path}", safe_filename(parts[-1]))
    elif host == "codeload.github.com" and len(parts) >= 4 and decoded[2] in {"zip", "tar.gz", "legacy.zip", "legacy.tar.gz"}:
        ext = ".zip" if "zip" in decoded[2] else ".tar.gz"
        return ArtifactTarget(platform, f"https://{host}/{path}", f"{safe_filename(parts[1])}{ext}")
    elif platform == "gitlab" and len(parts) >= 2:
        if "-" not in decoded and all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in decoded):
            project = quote("/".join(decoded), safe="")
            return ArtifactTarget(platform, f"https://gitlab.com/api/v4/projects/{project}/repository/archive.zip", f"{safe_filename(parts[-1])}.zip")
        if "-" in decoded:
            i = decoded.index("-")
            if i >= 2 and len(parts) > i + 2 and decoded[i + 1] in {"archive", "raw"}:
                return ArtifactTarget(platform, f"https://gitlab.com/{path}", safe_filename(parts[-1]))
            if i >= 2 and len(parts) > i + 3 and decoded[i + 1] == "blob":
                decoded[i + 1] = "raw"
                return ArtifactTarget(platform, "https://gitlab.com/" + "/".join(quote(p, safe="-._~") for p in decoded), safe_filename(parts[-1]))
    elif platform == "huggingface" and len(parts) >= 5:
        # Both model and dataset file links use /resolve/<revision>/<path>.
        i = 3 if decoded[0] in {"datasets", "spaces"} else 2
        if len(parts) > i + 2 and decoded[i] in {"blob", "resolve"}:
            decoded[i] = "resolve"
            return ArtifactTarget(platform, "https://huggingface.co/" + "/".join(quote(p, safe="-._~") for p in decoded), safe_filename(parts[-1]))
    elif platform == "pypi" and decoded[0] == "packages" and decoded[-1].endswith((".whl", ".tar.gz", ".zip")):
        return ArtifactTarget(platform, f"https://{host}/{path}", safe_filename(parts[-1]))
    raise InvalidURLError("Unsupported software link. Use a repository, source archive, raw file or direct release asset URL")
