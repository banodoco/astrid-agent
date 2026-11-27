"""Exact transfer regression tests: the pinned VibeComfy fetch runs against HTTP."""
import errno
import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from astrid.core.generation import asset_alignment, asset_gaps
from astrid.core.generation.engine_contracts import DownloadError, IntegrityError
from tests.core.generation.environment_states import serve_release, snapshot


DATA = bytes(range(256)) * 1024


def item(folder, url, data=DATA, name="asset.safetensors"):
    return {"folder": str(folder), "file": name, "url": url,
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "hard_links": True}


def test_real_interruption_range_and_repeat_safe(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    with serve_release({"model": DATA}, break_after=100000) as (url, server), httpx.Client() as client:
        row = item(folder, url + "/model")
        final = folder / row["file"]
        part = folder / ".asset.safetensors.astrid-resume"
        with pytest.raises(DownloadError):
            asset_gaps._fetch_into_folder(row, client)
        assert not final.exists()
        assert part.read_bytes() == DATA[:100000]
        assert list(folder.iterdir()) == [part]
        assert server.requests == [("/model", None)]
        assert asset_gaps._fetch_into_folder(row, client) == 100000
        assert server.requests[-1] == ("/model", "bytes=100000-")
        assert hashlib.sha256(final.read_bytes()).hexdigest() == row["sha256"]
        assert not part.exists()
        before = snapshot(folder)
        with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
            asset_gaps._fetch_into_folder(row, client)
        assert caught.value.code == "file_exists"
        assert snapshot(folder) == before


@pytest.mark.parametrize("bad", [b"x" * len(DATA), DATA + b"overflow"])
def test_real_mismatch_cleans_staging_and_part(tmp_path, bad):
    folder = tmp_path / "models"
    folder.mkdir()
    with serve_release({"model": bad}) as (url, _server), httpx.Client() as client:
        row = item(folder, url + "/model")
        with pytest.raises(IntegrityError) as caught:
            asset_gaps._fetch_into_folder(row, client)
        error = asset_gaps._stopped({"files": [row]}, [], row, caught.value)
        assert error.code == "checksum_mismatch"
        assert "refused" in str(error) and "partial copy was removed" in str(error)
        assert list(folder.iterdir()) == []


def test_malformed_range_never_consumes_retained_prefix(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    part = folder / ".asset.safetensors.astrid-resume"
    part.write_bytes(DATA[:8])
    row = item(folder, "https://huggingface.co/x/resolve/main/model")

    def respond(request):
        assert request.headers["range"] == "bytes=8-"
        return httpx.Response(206, headers={"Content-Range": f"bytes 8-{len(DATA) - 1}/{len(DATA) + 1}"},
                              content=DATA[8:])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError, match="different range"):
            asset_gaps._fetch_into_folder(row, client)
    assert list(folder.iterdir()) == [part] and part.read_bytes() == DATA[:8]


def test_running_total_is_cumulative_and_stderr_only(monkeypatch, capsys):
    monkeypatch.setattr(asset_gaps, "_PROGRESS_STEP", 1)
    first = asset_gaps._progress("one", 10, finished=0, grand_total=30)
    second = asset_gaps._progress("two", 20, finished=10, grand_total=30)
    first(4)
    first(10)
    second(5)
    second(20)
    second(20)  # completion callback may repeat, must not duplicate line
    out = capsys.readouterr()
    assert not out.out
    assert [line for line in out.err.splitlines() if line.startswith("Downloading models:")] == [
        "Downloading models: 4 bytes of 30 bytes", "Downloading models: 10 bytes of 30 bytes",
        "Downloading models: 15 bytes of 30 bytes", "Downloading models: 30 bytes of 30 bytes",
    ]


@pytest.mark.parametrize(("partial", "expected"), [([], None), ([4], 4), ([4, 6], 10)])
def test_resume_summary(partial, expected):
    plan = {"total_bytes": 30, "partial_bytes": [{"bytes": n} for n in partial]}
    summary = asset_gaps.resume_summary(plan)
    if expected is None:
        assert summary is None
    else:
        assert f"Picking up where we left off: {expected} bytes of 30 bytes" in summary
        assert "verified before publication" in summary


def test_disk_full_in_stream_is_distinct_and_does_not_publish(tmp_path, monkeypatch):
    folder = tmp_path / "models"
    folder.mkdir()
    original = asset_alignment._SplicedResponse.iter_bytes

    def interrupted(self):
        for block in original(self):
            yield block
            raise OSError(errno.ENOSPC, "test disk full")

    monkeypatch.setattr(asset_alignment._SplicedResponse, "iter_bytes", interrupted)
    with serve_release({"model": DATA}) as (url, _server), httpx.Client() as client:
        row = item(folder, url + "/model")
        with pytest.raises(DownloadError) as caught:
            asset_gaps._fetch_into_folder(row, client)
        error = asset_gaps._stopped({"files": [row]}, [], row, caught.value)
        assert error.code == "disk_full" and "Free disk space first" in str(error)
        assert not (folder / row["file"]).exists()
        assert list(folder.iterdir()) == [folder / ".asset.safetensors.astrid-resume"]


@pytest.mark.parametrize("retained", [False, True])
def test_apply_prints_resume_and_total_once(tmp_path, monkeypatch, capsys, retained):
    files = [item(tmp_path, "https://huggingface.co/x/resolve/main/one", b"a" * 10, "one"),
             item(tmp_path, "https://huggingface.co/x/resolve/main/two", b"b" * 20, "two")]
    plan = {"entry": "entry", "mode": "mode", "target": "http://127.0.0.1:8188",
            "files": files, "present": [], "manual": [], "total_bytes": 30,
            "partial_bytes": [{"file": "one", "bytes": 4}] if retained else [],
            "space_ok": True, "plan_sha256": "a" * 64}
    monkeypatch.setattr(asset_gaps, "plan_attached_model_download", lambda *a, **k: plan)
    monkeypatch.setattr(asset_gaps, "_hard_links_supported", lambda folder: True)
    from astrid.core.generation import targets
    monkeypatch.setattr(targets, "read_target", lambda: None)

    def fetch(row, client, *, progress):
        progress(row["bytes"])
        return 4 if retained and row["file"] == "one" else 0

    monkeypatch.setattr(asset_gaps, "_fetch_into_folder", fetch)
    client = SimpleNamespace(target=SimpleNamespace(url=plan["target"]))
    result = asset_gaps.apply_attached_model_download(client, "entry", "mode", plan_sha256="a" * 64)
    out = capsys.readouterr()
    assert not out.out and out.err.count("Picking up where we left off") == int(retained)
    assert [line for line in out.err.splitlines() if line.startswith("Downloading models:")] == [
        "Downloading models: 10 bytes of 30 bytes", "Downloading models: 30 bytes of 30 bytes"]
    assert result["total_bytes"] == 30


def test_no_hardlink_concurrent_final_is_never_overwritten(tmp_path, monkeypatch):
    staged, final = tmp_path / "staged", tmp_path / "asset.safetensors"
    staged.write_bytes(DATA)
    native = asset_gaps._rename_noreplace

    def competing_writer(source, destination):
        # The foreign file arrives after verification, at the actual native
        # publication boundary. Run the real exclusive rename, not a fake.
        destination.write_bytes(b"foreign file")
        return native(source, destination)

    monkeypatch.setattr(asset_gaps, "_rename_noreplace", competing_writer)
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        asset_gaps._publish(staged, final, hard_links=False)
    assert caught.value.code == "file_exists"
    assert final.read_bytes() == b"foreign file"
    assert staged.read_bytes() == DATA


def test_no_hardlink_native_success_never_creates_placeholder(tmp_path):
    staged, final = tmp_path / "staged", tmp_path / "asset.safetensors"
    staged.write_bytes(DATA)
    asset_gaps._publish(staged, final, hard_links=False)
    assert final.read_bytes() == DATA and not staged.exists()


@pytest.mark.parametrize("err", [errno.EIO, errno.ENOSPC, errno.ENOTSUP])
def test_no_hardlink_publication_failure_preserves_concurrent_foreign_file(tmp_path, monkeypatch, err):
    staged, final = tmp_path / "staged", tmp_path / "asset.safetensors"
    staged.write_bytes(DATA)

    def fail(source, destination):
        destination.write_bytes(b"foreign file")
        raise OSError(err, "native publication failed")

    monkeypatch.setattr(asset_gaps, "_rename_noreplace", fail)
    expected = asset_gaps.AttachedDownloadRefused if err == errno.ENOTSUP else OSError
    with pytest.raises(expected) as caught:
        asset_gaps._publish(staged, final, hard_links=False)
    if err == errno.ENOTSUP:
        assert caught.value.code == "atomic_publish_unavailable"
    else:
        assert caught.value.errno == err
    assert final.read_bytes() == b"foreign file" and staged.read_bytes() == DATA
