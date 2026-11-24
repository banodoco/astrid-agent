"""LE-8: download/storage warnings are not capacity admission."""
import time
from pathlib import Path

from astrid.core.generation import model_storage as storage


def _observe(root, volume=None):
    return storage.observe_download_storage(
        root, deadline=time.monotonic() + 10, check=lambda: None, volume=volume,
    )


def test_no_volume_warns_even_when_hint_exceeds_capacity(tmp_path, monkeypatch):
    # The recorded-answer observer represents a target with statvfs support.
    monkeypatch.setattr(storage.os, "statvfs", object(), raising=False)
    monkeypatch.setattr(storage, "available_model_bytes", lambda *a, **kw: (500, False))
    before = set(tmp_path.iterdir())
    observation = _observe(tmp_path)
    notice = storage.model_download_notice({"fixture-pack": 1000}, observation)
    assert notice["code"] == "model_download_notice"
    assert notice["severity"] == "warning"
    assert notice["fix_ids"] == []
    assert notice["details"] == {
        "packs": [{"pack": "fixture-pack", "size_hint_bytes": 1000}],
        "free_bytes": 500, "capacity_uncertain": False, "volume_status": "not_declared",
    }
    assert "no persistent volume declared" in notice["message"]
    assert "500 bytes reported free" in notice["message"]
    assert set(tmp_path.iterdir()) == before


def test_measured_volume_tightens_existing_capacity(tmp_path, monkeypatch):
    # The recorded-answer observer represents a target with statvfs support.
    monkeypatch.setattr(storage.os, "statvfs", object(), raising=False)
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
