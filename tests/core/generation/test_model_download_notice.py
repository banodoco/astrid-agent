"""LE-8: download/storage warnings are not capacity admission."""
import time
from pathlib import Path

import pytest

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
    with pytest.raises(storage.UnsafePathError, match="unsafe target path"):
        _observe(Path("models"))


# The VibeComfy #204 catalog is the authority, not an Agent-local registry.
SAM_REPO = "https://github.com/ethanfel/ComfyUI-SAM-Audio"
SAM_PIN = "3cfbd03da3019c7252ba13efd5694b1b2ff487e5"
SAM_CLASSES = {"SAMAudioPipelineLoader", "SAMAudioTextSeparator"}


def test_selected_pack_download_hints_use_catalog_and_exact_source():
    rows = [{"repo": SAM_REPO, "commit": SAM_PIN}]
    assert storage.selected_pack_download_hints(rows, SAM_CLASSES) == {
        "ComfyUI-SAM-Audio": 14_861_356_211,
    }
    assert storage.selected_pack_download_hints(rows, {"EmptyImage"}) == {}
    assert storage.selected_pack_download_hints([], SAM_CLASSES) == {}
    assert storage.selected_pack_download_hints(
        [{"repo": "https://github.com/example/ComfyUI-SAM-Audio", "commit": SAM_PIN}],
        SAM_CLASSES,
    ) == {}
    assert storage.selected_pack_download_hints(
        [{"repo": SAM_REPO.lower(), "commit": SAM_PIN}], SAM_CLASSES,
    ) == {}


def test_selected_local_target_observes_only_its_model_root(tmp_path, monkeypatch):
    models = tmp_path / "selected" / "models"
    models.mkdir(parents=True)
    seen = []

    def measure(root, **kwargs):
        seen.append(root)
        return 123, False

    monkeypatch.setattr(storage.os, "statvfs", object(), raising=False)
    monkeypatch.setattr(storage, "available_model_bytes", measure)
    before = set(models.iterdir())
    notice = storage.selected_target_download_notice(
        [{"repo": SAM_REPO, "commit": SAM_PIN}], SAM_CLASSES,
        placement="local", local_models_root=models,
    )
    assert notice["code"] == "model_download_notice"
    assert notice["severity"] == "warning"
    assert notice["fix_ids"] == []
    assert notice["details"]["free_bytes"] == 123
    assert notice["details"]["volume_status"] == "not_declared"
    assert seen == [models]
    assert set(models.iterdir()) == before


def test_remote_or_attached_download_notice_never_measures_controller(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("remote notice must not inspect local capacity")

    monkeypatch.setattr(storage, "available_model_bytes", forbidden)
    monkeypatch.setattr(storage, "measure_declared_volume", forbidden)
    for placement in ("runpod", "attached"):
        for declared, expected in ((None, "unknown"), (False, "not_declared"),
                                   (True, "declared_unverified")):
            notice = storage.selected_target_download_notice(
                [{"repo": SAM_REPO, "commit": SAM_PIN}], SAM_CLASSES,
                placement=placement, local_models_root=tmp_path,
                volume_declared=declared,
            )
            assert notice["details"]["free_bytes"] is None
            assert notice["details"]["capacity_uncertain"] is True
            assert notice["details"]["volume_status"] == expected
            assert notice["severity"] == "warning"
            assert notice["fix_ids"] == []


def test_missing_statvfs_is_unknown_not_an_unsafe_path(tmp_path, monkeypatch):
    monkeypatch.delattr(storage.os, "statvfs", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("statvfs unavailable; no capacity probe should run")

    monkeypatch.setattr(storage, "available_model_bytes", forbidden)
    assert _observe(tmp_path) == storage.DownloadStorageObservation(None, True, "not_declared")


@pytest.mark.parametrize("relative", [True, False])
@pytest.mark.parametrize("has_statvfs", [True, False])
def test_selected_unsafe_model_root_surfaces_as_typed_issue(tmp_path, monkeypatch, relative, has_statvfs):
    from astrid.core.generation.preflight import GenerationReadinessError

    # A file in place of the selected model directory is a real unsafe
    # target path, even if the platform has no usable statvfs implementation.
    root = Path("models") if relative else tmp_path / "not-a-directory"
    if not relative:
        root.write_bytes(b"do not overwrite")
    if has_statvfs:
        monkeypatch.setattr(storage.os, "statvfs", object(), raising=False)
    else:
        monkeypatch.delattr(storage.os, "statvfs", raising=False)
    with pytest.raises(GenerationReadinessError) as caught:
        storage.selected_target_download_notice(
            [{"repo": SAM_REPO, "commit": SAM_PIN}], SAM_CLASSES,
            placement="local", local_models_root=root,
        )
    result = caught.value.to_envelope()
    assert result["status"] == "blocked"
    issue, = result["issues"]
    assert issue["code"] == "unsafe_path"
    assert issue["details"]["field"] == "models_root"
    assert "unsafe target path" in issue["message"]
    assert not any(row["code"] == "model_download_notice" for row in result["issues"])
    if not relative:
        assert root.read_bytes() == b"do not overwrite"
