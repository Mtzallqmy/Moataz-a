import io
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.services import download_process
from app.services.downloader import _YtDlpEngine


@pytest.mark.parametrize("quality", ["best", "720", "audio"])
def test_child_download_passes_required_engine_arguments(monkeypatch, capsys, tmp_path, quality):
    output = tmp_path / ("clip.mp3" if quality == "audio" else "clip.mp4")
    output.write_bytes(b"media")

    def download(self, url, selected_quality, job_dir, *, cancel_event,
                 progress_hook=None, known_qualities=None):
        assert url == "https://example.com/clip"
        assert selected_quality == quality
        assert job_dir == tmp_path
        # The parent watches cancellation and kills the child process group.
        assert cancel_event is None
        assert known_qualities == [720]
        progress_hook({"status": "finished", "downloaded_bytes": 5, "filename": "private"})
        return output

    monkeypatch.setattr(_YtDlpEngine, "download", download)
    monkeypatch.setattr(download_process.logging, "disable", lambda level: None)
    payload = {
        "settings": download_process._settings_payload(Settings(_env_file=None)),
        "operation": "download", "url": "https://example.com/clip",
        "quality": quality, "output_dir": str(tmp_path), "known_qualities": [720],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    download_process.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [
        {"type": "progress", "data": {"status": "finished", "downloaded_bytes": 5}},
        {"type": "result", "data": {"output": str(Path(output))}},
    ]
