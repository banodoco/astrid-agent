"""LE-8: download/storage warnings are not capacity admission."""
import time
from pathlib import Path

import pytest

from astrid.core.generation import model_storage as storage


def _observe(root, volume=None):
    return storage.observe_download_storage(
        root, deadline=time.monotonic() + 10, check=lambda: None, volume=volume,
    )


@pytest.mark.skipif(not hasattr(storage.os, "statvfs"), reason="target capacity observer uses statvfs")
def test_no_volume_warns_even_when_hint_exceeds_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "available_model_bytes", lambda *a, **kw: (500, False))
    observation = _observe(tmp_path)
    notice = storage.model_download_notice({"fixture-pack": 1000}, observation)
    assert notice["severity"] == "warning"
    assert notice["details"] == {
        "packs": [{"pack": "fixture-pack", "size_hint_bytes": 1000}],
        "free_bytes": 500, "capacity_uncertain": False, "volume_status": "not_declared",
    }
    assert "no persistent volume declared" in notice["message"]
    assert "500 bytes reported free" in notice["message"]
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not hasattr(storage.os, "statvfs"), reason="target capacity observer uses statvfs")
def test_measured_volume_tightens_existing_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "available_model_bytes", lambda *a, **kw: (5000, False))
    fact = storage.VolumeSpace(tmp_path, tmp_path.stat().st_dev, 2500)
    monkeypatch.setattr(storage, "measure_declared_volume", lambda *a, **kw: fact)
    assert _observe(tmp_path, storage.DeclaredVolume(tmp_path, 1)) == (
        storage.DownloadStorageObservation(2500, False, "declared_measured")
    )


def test_unavailable_remote_facts_remain_unknown():
    observation = storage.DownloadStorageObservation(None, True, "unknown")
    notice = storage.model_download_notice({"fixture-pack": None}, observation)
    assert notice["details"]["free_bytes"] is None
    assert notice["details"]["volume_status"] == "unknown"
    assert notice["details"]["packs"][0]["size_hint_bytes"] is None
    assert "download size unknown" in notice["message"]
    assert "target free space unknown" in notice["message"]
    assert storage.model_download_notice({}, observation) is None


def test_relative_root_never_measures_controller_disk(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("relative path must not be measured")
    monkeypatch.setattr(storage, "available_model_bytes", forbidden)
    assert _observe(Path("models")) == storage.DownloadStorageObservation(None, True, "not_declared")
