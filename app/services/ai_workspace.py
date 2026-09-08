from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import aiohttp

from app.errors import InvalidURLError
from app.security import assert_public_dns

MAX_PROJECT_FILES = 100
MAX_SINGLE_FILE_BYTES = 512 * 1024
MAX_PROJECT_BYTES = 5 * 1024 * 1024
MAX_FETCH_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_CHARS = 350_000
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml", ".yaml", ".yml",
    ".html", ".htm", ".css", ".scss", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb",
    ".swift", ".dart", ".vue", ".svelte", ".xml", ".ini", ".cfg", ".conf", ".env", ".dockerfile",
}
_IGNORED_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache"}


class WorkspaceError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001, ARG002
        if tag.lower() in {"script", "style", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


@dataclass(frozen=True, slots=True)
class ProjectArtifact:
    name: str
    directory: Path
    zip_path: Path
    file_count: int
    total_bytes: int


def _safe_name(value: object, default: str = "ai-project") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
    return (text[:64] or default).lower()


def _safe_relative_path(value: object) -> PurePosixPath:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise WorkspaceError("Unsafe project file path")
    if any(part in {"", "."} or part.lower() in _IGNORED_PARTS for part in path.parts):
        raise WorkspaceError("Unsafe or ignored project file path")
    if len(path.parts) > 16 or len(raw) > 240:
        raise WorkspaceError("Project file path is too deep or too long")
    return path


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _is_text_candidate(path: PurePosixPath) -> bool:
    name = path.name.lower()
    if name in {"dockerfile", "makefile", "procfile", "gemfile", "requirements.txt", "package.json"}:
        return True
    return path.suffix.lower() in _TEXT_EXTENSIONS


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def _parse_json_object(text: str) -> dict[str, object]:
    candidate = _strip_code_fence(text)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise WorkspaceError("AI response did not contain a project JSON object") from None
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise WorkspaceError("AI response contained invalid project JSON") from exc
    if not isinstance(payload, dict):
        raise WorkspaceError("AI project payload must be a JSON object")
    return payload


def parse_project_files(text: str) -> tuple[str, list[tuple[PurePosixPath, str]]]:
    payload = _parse_json_object(text)
    name = _safe_name(payload.get("name"))
    raw_files = payload.get("files")
    files: list[tuple[PurePosixPath, str]] = []

    if isinstance(raw_files, dict):
        iterable = [{"path": key, "content": value} for key, value in raw_files.items()]
    elif isinstance(raw_files, list):
        iterable = raw_files
    else:
        raise WorkspaceError("AI project payload must contain a files list or mapping")

    total = 0
    seen: set[PurePosixPath] = set()
    for raw in iterable:
        if not isinstance(raw, dict):
            continue
        path = _safe_relative_path(raw.get("path"))
        content = raw.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_SINGLE_FILE_BYTES:
            raise WorkspaceError(f"Project file is too large: {path}")
        total += len(encoded)
        if total > MAX_PROJECT_BYTES:
            raise WorkspaceError("Generated project exceeds the workspace size limit")
        if path in seen:
            continue
        seen.add(path)
        files.append((path, content))
        if len(files) > MAX_PROJECT_FILES:
            raise WorkspaceError("Generated project contains too many files")

    if not files:
        raise WorkspaceError("AI project payload contained no files")
    return name, files


def build_project_zip(text: str, root: Path) -> ProjectArtifact:
    name, files = parse_project_files(text)
    workspace = root / "ai-projects" / f"{name}-{uuid4().hex[:10]}"
    workspace.mkdir(parents=True, exist_ok=False)
    total = 0
    try:
        for relative, content in files:
            destination = workspace.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            total += len(content.encode("utf-8"))
        zip_path = workspace.parent / f"{workspace.name}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative, _ in files:
                source = workspace.joinpath(*relative.parts)
                archive.write(source, arcname=str(PurePosixPath(name) / relative))
        return ProjectArtifact(name=name, directory=workspace, zip_path=zip_path, file_count=len(files), total_bytes=total)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def cleanup_artifact(artifact: ProjectArtifact) -> None:
    shutil.rmtree(artifact.directory, ignore_errors=True)
    artifact.zip_path.unlink(missing_ok=True)


def summarize_zip_bytes(data: bytes, label: str = "archive.zip") -> str:
    if len(data) > MAX_FETCH_BYTES:
        raise WorkspaceError("Archive is too large to inspect")
    sections: list[str] = []
    total_chars = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) > MAX_PROJECT_FILES * 4:
            raise WorkspaceError("Archive contains too many files")
        for item in entries:
            path = _safe_relative_path(item.filename)
            if any(part.lower() in _IGNORED_PARTS for part in path.parts) or not _is_text_candidate(path):
                continue
            if item.file_size > MAX_SINGLE_FILE_BYTES:
                continue
            with archive.open(item, "r") as source:
                text = _decode_text(source.read(MAX_SINGLE_FILE_BYTES + 1))
            if text is None:
                continue
            block = f"\n--- FILE: {path} ---\n{text}\n"
            if total_chars + len(block) > MAX_CONTEXT_CHARS:
                break
            sections.append(block)
            total_chars += len(block)
    if not sections:
        raise WorkspaceError("No readable source files were found in the archive")
    return f"SOURCE ARCHIVE: {label}\n" + "".join(sections)


def summarize_file_bytes(data: bytes, filename: str) -> str:
    if len(data) > MAX_SINGLE_FILE_BYTES:
        raise WorkspaceError("File is too large to inspect")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".zip":
        return summarize_zip_bytes(data, filename)
    text = _decode_text(data)
    if text is None:
        raise WorkspaceError("This file format is not readable as source text")
    return f"SOURCE FILE: {filename}\n\n{text[:MAX_CONTEXT_CHARS]}"


async def _download_url(url: str, *, max_bytes: int = MAX_FETCH_BYTES, accept_binary: bool = False) -> tuple[bytes, str]:
    current = assert_public_dns(url)
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "MoatazBot/1.0", "Accept": "*/*" if accept_binary else "text/html,application/json,text/plain,*/*;q=0.2"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for _ in range(4):
            async with session.get(current, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise WorkspaceError("URL redirect did not include a destination")
                    current = assert_public_dns(urljoin(current, location))
                    continue
                if response.status >= 400:
                    raise WorkspaceError(f"URL returned HTTP {response.status}")
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise WorkspaceError("Remote resource is too large")
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise WorkspaceError("Remote resource exceeded the download limit")
                return bytes(buffer), str(response.headers.get("Content-Type") or "")
    raise WorkspaceError("Too many URL redirects")


def _github_repo_parts(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        return None
    return owner, repo


async def fetch_github_repo_context(url: str) -> str:
    parts = _github_repo_parts(url)
    if parts is None:
        raise WorkspaceError("Not a GitHub repository URL")
    owner, repo = parts
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    metadata_bytes, _ = await _download_url(api_url, max_bytes=512 * 1024)
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        default_branch = str(metadata.get("default_branch") or "main")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise WorkspaceError("Could not read GitHub repository metadata") from exc
    archive_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{default_branch}"
    archive_bytes, _ = await _download_url(archive_url, max_bytes=MAX_FETCH_BYTES, accept_binary=True)
    return summarize_zip_bytes(archive_bytes, f"{owner}/{repo}@{default_branch}")


async def fetch_web_context(url: str) -> str:
    repo = _github_repo_parts(url)
    if repo is not None and len([part for part in urlsplit(url).path.split("/") if part]) == 2:
        return await fetch_github_repo_context(url)
    data, content_type = await _download_url(url)
    text = _decode_text(data)
    if text is None:
        raise WorkspaceError("Remote resource is not readable text")
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        parser = _TextExtractor()
        parser.feed(text)
        text = "\n".join(parser.parts)
    return f"SOURCE URL: {url}\n\n{text[:MAX_CONTEXT_CHARS]}"


async def read_source_url(url: str) -> str:
    try:
        return await fetch_web_context(url)
    except InvalidURLError as exc:
        raise WorkspaceError(str(exc)) from exc


PROJECT_SYSTEM_PROMPT = """You are a senior software engineer producing a complete project artifact.
Return ONLY valid JSON, with no markdown fences and no commentary, using this exact shape:
{"name":"project-name","files":[{"path":"relative/path.ext","content":"complete file contents"}]}
Requirements:
- Build a coherent runnable project, not snippets.
- Include all essential source/config files and a concise README.
- Never include secrets, API keys, credentials, .env values, vendored dependencies, binary files, node_modules, .git, build outputs, or lockfiles containing private registries.
- Paths must be relative and must not contain .. or absolute paths.
- Prefer small maintainable files; keep the whole project reasonably compact.
- If SOURCE CONTEXT is provided by the user, preserve relevant existing behavior and apply requested edits instead of discarding the project.
"""
