"""Linux diagnostic bootstrap isolates host media tools without changing the pin."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from astrid.installer import tools
from scripts import ci_journey as journey, ci_journey_keepgoing as keepgoing
from tests.test_ci_journey_keepgoing import setup_diagnostic


pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux diagnostic")


@pytest.mark.parametrize("bootstrap_fails", [False, True])
def test_shims_only_cover_fresh_bootstrap_and_are_removed(tmp_path, monkeypatch, bootstrap_fails):
    args, evidence, calls = setup_diagnostic(tmp_path, monkeypatch, "none")
    args.leg = "fresh-install"
    original_path = os.environ.get("PATH")
    observed = {}

    def bootstrap(*args, path_prefix):
        observed["directory"] = path_prefix
        assert os.environ.get("PATH") == original_path
        for name in ("ffmpeg", "ffprobe"):
            executable = Path(shutil.which(name, path=str(path_prefix) + os.pathsep + os.defpath))
            assert executable == path_prefix / name
            assert subprocess.run([executable, "-version"], check=False).returncode != 0
        if bootstrap_fails:
            raise RuntimeError("bootstrap failed")

    def receipt(*args):
        assert not observed["directory"].exists()
        assert os.environ.get("PATH") == original_path
        calls.append("receipt")

    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(keepgoing.ffmpeg_receipt, "capture", receipt)
    report = keepgoing.execute(args, tmp_path / "program", tmp_path / "data", evidence, SimpleNamespace(pid=1), "birth")
    rows = {row["stage"]: row for row in report["stages"]}
    assert rows["release1-install"]["result"] == ("fail" if bootstrap_fails else "pass")
    assert rows["release1-ffmpeg-receipt"]["result"] == "pass"
    assert "receipt" in calls
    assert not observed["directory"].exists()
    assert os.environ.get("PATH") == original_path


def test_bootstrap_passes_prefix_only_to_child(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    (app / "public/install").write_text('#!/bin/sh\ncommand -v ffmpeg\nffmpeg -version\n', encoding="utf-8")
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "ffmpeg").write_text('#!/bin/sh\nexit 17\n', encoding="utf-8")
    (shims / "ffmpeg").chmod(0o755)
    original_path = os.environ.get("PATH")
    with pytest.raises(RuntimeError, match="exit 17"):
        journey.bootstrap(app, tmp_path / "manifest", "sh", "install", tmp_path / "evidence",
                          tmp_path / "root", tmp_path / "data", path_prefix=shims)
    assert (tmp_path / "evidence/install-bootstrap.log").read_text().strip() == str(shims / "ffmpeg")
    assert os.environ.get("PATH") == original_path


@pytest.mark.parametrize("bad_checksum", [False, True])
def test_candidate_media_selection_downloads_and_enforces_checksum(tmp_path, monkeypatch, bad_checksum):
    # The archive is served in memory; the installer's real checksum and extraction run.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in ("ffmpeg", "ffprobe"):
            capabilities = " ".join(sorted(tools.ENCODERS | tools.FILTERS))
            script = (f'#!/bin/sh\nif [ "$1" = "-version" ]; then echo "{name} version 7.0.2"; '
                      f'else for item in {capabilities}; do echo " X $item"; done; fi\n')
            bundle.writestr(name, script)
    content = archive.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    url = "https://example.test/pinned.zip"
    pin = {"assets": {"linux-x64": {"url": url, "sha256": "0" * 64 if bad_checksum else digest}}}
    requested = []
    def urlopen(request, timeout):
        requested.append(request.full_url)
        return io.BytesIO(content)
    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    host = tmp_path / "host"
    host.mkdir()
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        for name in ("ffmpeg", "ffprobe"):
            executable = host / name
            executable.write_bytes(bundle.read(name))
            executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(host) + os.pathsep + os.defpath)
    def run(argv):
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    root = tmp_path / "versions" / "v1" / "tools"
    assert tools._media_tools(pin, root, "linux-x64", run)["ASTRID_INSTALL_FFMPEG_SOURCE"] == "path"
    assert requested == []

    args, evidence, _ = setup_diagnostic(tmp_path, monkeypatch, "none")
    args.leg = "fresh-install"
    def bootstrap(*args, path_prefix):
        with monkeypatch.context() as scoped:
            scoped.setenv("PATH", str(path_prefix) + os.pathsep + os.environ["PATH"])
            result = tools._media_tools(pin, root, "linux-x64", run)
            assert result["ASTRID_INSTALL_FFMPEG_SOURCE"] == "download"
            assert Path(result["ASTRID_FFMPEG_EXECUTABLE"]).is_relative_to(root / "ffmpeg")
            assert Path(result["ASTRID_FFPROBE_EXECUTABLE"]).is_relative_to(root / "ffmpeg")
    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(keepgoing.ffmpeg_receipt, "capture", lambda *args: None)
    report = keepgoing.execute(args, tmp_path / "program", tmp_path / "data", evidence, SimpleNamespace(pid=1), "birth")
    row = next(row for row in report["stages"] if row["stage"] == "release1-install")
    assert row["result"] == ("fail" if bad_checksum else "pass")
    if bad_checksum:
        assert "checksum mismatch" in row["first_error"]
        assert not (root / "ffmpeg.download").exists()
    assert requested == [url]
