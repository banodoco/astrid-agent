"""LE-10: saved API pin reaches the actual CPU-target dry-run response."""
from __future__ import annotations

import json
import wave

import pytest

from astrid.core.generation import cli, preflight
from astrid.core.generation.node_approvals import approval_record, write_entry_approvals
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)
from tests.core.generation.fixtures.plan56_sam import (  # noqa: F401
    sam_api_path, sam_object_info, sam_offline_lookup,
)
from vibecomfy.entries import import_as_entry

REPO = "https://github.com/ethanfel/ComfyUI-SAM-Audio"
PIN = "3cfbd03da3019c7252ba13efd5694b1b2ff487e5"


def test_saved_explicit_api_pin_notice_reaches_cli_dry_run(
        network, monkeypatch, tmp_path, capsys, sam_api_path):
    client, requests = install_target(network, sam_object_info())
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available",
                        lambda: pytest.fail("managed installation probed"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry run opened Runtime"))
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    import_as_entry(sam_api_path, "sam-pinned", root, added_node_refs=[{
        "slug": "ComfyUI-SAM-Audio", "source": "git", "url": REPO, "commit": PIN,
        "class_set": ["SAMAudioPipelineLoader", "SAMAudioTextSeparator"],
    }])
    # An already approved exact fixture pin remains valid after LE-2b lands.
    write_entry_approvals(root / "sam-pinned", [
        approval_record({"repo": REPO, "commit": PIN}, placement_class="attached"),
    ])
    manifest = root / "sam-pinned" / "entry.json"
    before = manifest.read_bytes()
    audio = tmp_path / "clip.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\0\0" * 48)
    assert cli.main(["audio", "--entry", "sam-pinned", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", client.target.url,
                     "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    notice = next(row for row in result["entry"]["pack_version_notices"]
                  if row["details"]["pack"] == "ComfyUI-SAM-Audio")
    assert notice["message"] == f"ComfyUI-SAM-Audio: version not recorded; pinned at {PIN} by you"
    assert notice["details"]["recorded_versions"] == []
    assert not any(row["code"] == "pack_version_differs" for row in result["issues"])
    assert ("GET", "/object_info") in requests
    assert manifest.read_bytes() == before


def test_saved_explicit_api_pin_notice_reaches_target_schema(
        network, tmp_path, sam_api_path):
    """Exercise the saved source and live CPU inventory without the held SDK writer."""
    from astrid.core.generation import entries
    from astrid.sdk.generation import _explicit_workflow_needs
    from vibecomfy.entries import load_entry
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle

    client, requests = install_target(network, sam_object_info())
    root = tmp_path / "target-schema-entries"
    import_as_entry(sam_api_path, "sam-pinned", root, added_node_refs=[{
        "slug": "ComfyUI-SAM-Audio", "source": "git", "url": REPO, "commit": PIN,
        "class_set": ["SAMAudioPipelineLoader", "SAMAudioTextSeparator"],
    }])
    manifest = root / "sam-pinned" / "entry.json"
    before = manifest.read_bytes()
    entry = load_entry(manifest)
    graph = load_bundle(entries.bundle_path(entry), trust=Provenance.USER_CONFIRMED).workflow
    entry["needs"] = _explicit_workflow_needs(entry["needs"])
    notices = []
    assert entries.target_schema(entry, graph, client, version_notices=notices) is not None
    notice = next(row for row in notices if row["details"]["pack"] == "ComfyUI-SAM-Audio")
    assert notice["message"] == f"ComfyUI-SAM-Audio: version not recorded; pinned at {PIN} by you"
    assert notice["details"]["recorded_versions"] == []
    assert notice["issue"] is None
    assert ("GET", "/object_info") in requests
    assert manifest.read_bytes() == before
