import io
import json
import zipfile

import pytest

from app.services.ai_workspace import (
    WorkspaceError,
    build_project_zip,
    cleanup_artifact,
    parse_project_files,
    summarize_file_bytes,
)


def test_project_payload_builds_ready_zip(tmp_path):
    payload = json.dumps(
        {
            "name": "Demo App",
            "files": [
                {"path": "README.md", "content": "# Demo"},
                {"path": "src/main.py", "content": "print('ok')\n"},
            ],
        }
    )

    artifact = build_project_zip(payload, tmp_path)
    try:
        assert artifact.file_count == 2
        assert artifact.zip_path.exists()
        with zipfile.ZipFile(artifact.zip_path) as archive:
            names = set(archive.namelist())
        assert "demo-app/README.md" in names
        assert "demo-app/src/main.py" in names
    finally:
        cleanup_artifact(artifact)


def test_project_payload_rejects_path_traversal():
    payload = json.dumps(
        {
            "name": "bad",
            "files": [{"path": "../secret.txt", "content": "nope"}],
        }
    )

    with pytest.raises(WorkspaceError, match="Unsafe"):
        parse_project_files(payload)


def test_source_zip_reader_keeps_text_and_ignores_binary():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("repo/main.py", "print('hello')\n")
        archive.writestr("repo/image.png", b"\x89PNG\x00binary")

    context = summarize_file_bytes(buffer.getvalue(), "repo.zip")

    assert "repo/main.py" in context
    assert "print('hello')" in context
    assert "image.png" not in context


def test_plain_source_file_reader():
    context = summarize_file_bytes(b"const answer = 42;\n", "index.js")
    assert "SOURCE FILE: index.js" in context
    assert "answer = 42" in context
