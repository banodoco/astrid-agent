"""LE-8 acceptance: actual saved-entry CLI dry runs with the real catalog.

No pack code is executed; only CPU object_info and recorded policy/lease facts.
"""
from __future__ import annotations

import json
import wave

import pytest

from astrid.core.generation import cli, model_storage, placement as placements, preflight
from astrid.core.generation.node_approvals import approval_record, write_entry_approvals
from astrid.packs.runpod.target_selection import save_runpod_preferences
from astrid.sdk import generation
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)
from tests.core.generation.fixtures.plan56_sam import (  # noqa: F401
    sam_api_path, sam_object_info, sam_offline_lookup,
)
from vibecomfy.entries import import_as_entry

SAM_REPO = "https://github.com/ethanfel/ComfyUI-SAM-Audio"
SAM_PIN = "3cfbd03da3019c7252ba13efd5694b1b2ff487e5"


def _saved_sam(tmp_path, monkeypatch, source, placement_class):
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    import_as_entry(source, "sam-download", root, added_node_refs=[{
        "slug": "ComfyUI-SAM-Audio", "source": "git", "url": SAM_REPO,
        "commit": SAM_PIN,
        "class_set": ["SAMAudioPipelineLoader", "SAMAudioTextSeparator"],
    }])
    write_entry_approvals(root / "sam-download", [
        approval_record({"repo": SAM_REPO, "commit": SAM_PIN},
                        placement_class=placement_class),
    ])
    audio = tmp_path / "clip.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\0\0" * 48)
    return root, audio


def _entry_bytes(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _assert_notice(result, volume_status):
    assert result["status"] == "ready_with_warnings", result
    issue, = [row for row in result["issues"]
              if row["code"] == "model_download_notice"]
    assert issue["severity"] == "warning"
    assert issue["fix_ids"] == []
    assert issue["details"]["packs"] == [
        {"pack": "ComfyUI-SAM-Audio", "size_hint_bytes": 14_861_356_211},
    ]
    assert issue["details"]["free_bytes"] is None
    assert issue["details"]["volume_status"] == volume_status
    assert "target free space unknown" in issue["message"]
    assert result["fixes"] == []
    return issue


def _forbid_controller_capacity(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("remote dry run measured controller disk")

    monkeypatch.setattr(model_storage, "available_model_bytes", forbidden)
    monkeypatch.setattr(model_storage, "measure_declared_volume", forbidden)
    monkeypatch.setattr(cli, "_open_client",
                        lambda: pytest.fail("dry run opened Runtime"))


def test_attached_dry_run_reports_catalog_hint_without_local_capacity(
        network, monkeypatch, tmp_path, capsys, sam_api_path):
    client, requests = install_target(network, sam_object_info())
    _forbid_controller_capacity(monkeypatch)
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    root, audio = _saved_sam(tmp_path, monkeypatch, sam_api_path, "attached")
    before = _entry_bytes(root)
    source_before = sam_api_path.read_bytes()
    assert cli.main(["audio", "--entry", "sam-download", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", client.target.url,
                     "--dry-run", "--json"]) == 0
    _assert_notice(json.loads(capsys.readouterr().out), "unknown")
    assert ("GET", "/object_info") in requests
    assert all(method == "GET" for method, _ in requests)
    assert _entry_bytes(root) == before
    assert sam_api_path.read_bytes() == source_before


def test_pending_runpod_dry_run_without_declared_volume_warns_not_blocks(
        monkeypatch, tmp_path, capsys, sam_api_path):
    _forbid_controller_capacity(monkeypatch)
    monkeypatch.setattr(placements, "_runpod_leases", lambda kind: [])
    monkeypatch.setattr(generation, "_require_claimed_machine",
                        lambda *a, **k: pytest.fail("dry run required a paid machine"))
    # Explicit ephemeral policy for a future claim, not evidence about a
    # different retained machine. No provider call or storage mount.
    save_runpod_preferences({
        "provider_account_ref": "cpu-fixture", "gpu_types": ["CPU stand-in"],
        "image": "fixture/image@sha256:" + "a" * 64,
        "volume": {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"},
        "budget_cap_usd": 1.0,
    })
    root, audio = _saved_sam(tmp_path, monkeypatch, sam_api_path, "runpod")
    before = _entry_bytes(root)
    source_before = sam_api_path.read_bytes()
    assert cli.main(["audio", "--entry", "sam-download", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", "runpod",
                     "--dry-run", "--json"]) == 0
    issue = _assert_notice(json.loads(capsys.readouterr().out), "not_declared")
    assert "no persistent volume declared" in issue["message"]
    assert _entry_bytes(root) == before
    assert sam_api_path.read_bytes() == source_before


@pytest.mark.parametrize("retained,expected", [(False, "declared_unverified"), (True, "unknown")])
def test_runpod_dry_run_reports_only_selected_volume_policy(
        monkeypatch, tmp_path, capsys, sam_api_path, retained, expected):
    _forbid_controller_capacity(monkeypatch)
    monkeypatch.setattr(placements, "_runpod_leases", lambda kind: (
        [{"machine": "retained-fixture", "live": True, "state": "idle"}] if retained else []))
    monkeypatch.setattr(generation, "_require_claimed_machine",
                        lambda *a, **k: pytest.fail("dry run required a paid machine"))
    save_runpod_preferences({
        "provider_account_ref": "cpu-fixture", "gpu_types": ["CPU stand-in"],
        "image": "fixture/image@sha256:" + "a" * 64,
        "volume": {"id": "future-volume", "name": "Future", "size_gb": 40, "mount_path": "/workspace"},
        "budget_cap_usd": 1.0,
    })
    root, audio = _saved_sam(tmp_path, monkeypatch, sam_api_path, "runpod")
    before = _entry_bytes(root)
    source_before = sam_api_path.read_bytes()
    assert cli.main(["audio", "--entry", "sam-download", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", "runpod",
                     "--dry-run", "--json"]) == 0
    issue = _assert_notice(json.loads(capsys.readouterr().out), expected)
    assert "no persistent volume declared" not in issue["message"]
    assert _entry_bytes(root) == before
    assert sam_api_path.read_bytes() == source_before


def test_managed_dry_run_reports_selected_model_root_capacity(
        monkeypatch, tmp_path, capsys, sam_api_path):
    from astrid.core.generation import managed_preparation

    models = tmp_path / "managed" / "models"
    models.mkdir(parents=True)
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_registered_managed_engine", lambda: {
        "owner": "astrid", "endpoint": "http://127.0.0.1:8188",
        "root": str(models.parent),
    })
    monkeypatch.setattr(managed_preparation, "configured_models_root", lambda: str(models))
    monkeypatch.setattr(cli, "_open_client",
                        lambda: pytest.fail("dry run opened Runtime"))
    monkeypatch.setattr(model_storage.os, "statvfs", object(), raising=False)
    seen = []

    def measure(root, **kwargs):
        seen.append(root)
        return 123, False

    monkeypatch.setattr(model_storage, "available_model_bytes", measure)
    root, audio = _saved_sam(tmp_path, monkeypatch, sam_api_path, "local")
    before = _entry_bytes(root)
    source_before = sam_api_path.read_bytes()
    assert cli.main(["audio", "--entry", "sam-download", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", "managed",
                     "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready_with_warnings", result
    issue, = [row for row in result["issues"] if row["code"] == "model_download_notice"]
    assert issue["severity"] == "warning"
    assert issue["fix_ids"] == []
    assert issue["details"]["free_bytes"] == 123
    assert issue["details"]["volume_status"] == "not_declared"
    assert result["fixes"] == []
    assert seen == [models]
    assert not list(models.iterdir())
    assert _entry_bytes(root) == before
    assert sam_api_path.read_bytes() == source_before
