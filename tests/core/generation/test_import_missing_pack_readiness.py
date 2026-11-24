"""WS-0g: actual SAM import evidence survives to placement readiness."""
from __future__ import annotations

import json

import pytest

from astrid.core.generation import cli, entries, preflight
from astrid.packs.vibecomfy.production_engine import load_workflow_path
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)
from tests.core.generation.fixtures.plan56_sam import (  # noqa: F401
    sam_api_path, sam_object_info, sam_offline_lookup,
)

SAM_CLASSES = {"SAMAudioPipelineLoader", "SAMAudioTextSeparator"}


def test_import_report_survives_without_mutating_canonical_revision(tmp_path, sam_api_path):
    loaded = load_workflow_path(sam_api_path, tmp_path / "canonical")
    graph = loaded.resolved.workflow
    report = loaded.import_report
    assert report is not None
    assert not hasattr(graph, "_astrid_import_report")
    reported = {row.get("class_type") for row in report["diagnostics"]
                if row["code"] in {"unresolved_runtime_class", "unknown_class_type"}}
    assert SAM_CLASSES <= reported
    assert loaded.resolved.revision_id == report["revision_id"]
    assert "_astrid_import_report" not in graph.metadata


@pytest.mark.parametrize("placement", ["runpod", "local"])
def test_sam_deferred_target_dry_run_blocks_both_missing_classes(
    placement, monkeypatch, tmp_path, capsys, sam_api_path,
):
    from astrid.core.generation import placement as placements
    from astrid.sdk import generation

    monkeypatch.setattr(placements, "_runpod_leases", lambda kind: [])
    # Model the no-managed-engine case regardless of the proof runner's install.
    monkeypatch.setattr(preflight, "_registered_managed_engine", lambda: None)
    monkeypatch.setattr(generation, "_require_claimed_machine",
                        lambda *a, **k: pytest.fail("dry run required a claimed machine"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry run opened Runtime"))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    original = sam_api_path.read_bytes()
    assert cli.main(["workflow", str(sam_api_path), "--placement", placement,
                     "--dry-run", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert {issue["details"]["node"] for issue in result["issues"]
            if issue["code"] == "missing_node_pack"} == SAM_CLASSES
    assert result["target"]["placement"] == placement
    engine_issues = [issue for issue in result["issues"] if issue["code"] == "engine_not_found"]
    assert len(engine_issues) == (1 if placement == "local" else 0)
    assert result["fixes"], "blocked readiness must retain actionable guidance"
    assert sam_api_path.read_bytes() == original
    assert not (tmp_path / "entries").exists()


def test_cached_custom_schema_does_not_make_runpod_ready(monkeypatch, sam_api_path, tmp_path):
    from dataclasses import replace

    from vibecomfy.entries import draft_entry
    from vibecomfy.schema.provider import ObjectInfoIndexSchemaProvider, TargetSchemaProvider
    from astrid.core.generation.preflight import GenerationReadinessError
    from astrid.sdk.generation import _explicit_workflow_needs

    graph = load_workflow_path(sam_api_path, tmp_path / "cached-canonical").resolved.workflow
    entry = draft_entry(graph, "sam")
    entry["needs"] = _explicit_workflow_needs(entry["needs"])
    # Feed non-core cache evidence to the actual shipped-index lookup used by
    # readiness, not the separate authoring provider. A cached schema still
    # cannot establish a custom-node installable provider.
    ambient = TargetSchemaProvider.from_object_info(
        sam_object_info(), server_url="http://127.0.0.1:8188", model_availability="target",
    )
    original = ObjectInfoIndexSchemaProvider.get_schema
    seen = set()

    def cached(provider, name):
        if name in SAM_CLASSES:
            seen.add(name)
            return replace(ambient.get_schema(name), source_provider="object_info_index",
                           source_cache_path=str(provider.root / "unreviewed-custom@fixture.json"))
        return original(provider, name)

    monkeypatch.setattr(ObjectInfoIndexSchemaProvider, "get_schema", cached)

    # Core SaveAudio is deliberately outside VibeComfy's legacy explicit
    # CORE_COMFY_CLASSES set; the shipped core index must still cover it.
    pending = {"engine": "comfyui", "placement": "runpod", "state": "pending_host_readiness"}
    with pytest.raises(GenerationReadinessError) as caught:
        entries.target_schema(entry, graph, None, pending_target=pending)
    readiness = caught.value.readiness
    assert readiness["status"] == "blocked"
    assert {issue["details"]["node"] for issue in readiness["issues"]
            if issue["code"] == "missing_node_pack"} == SAM_CLASSES
    assert readiness["target"] == pending
    assert seen == SAM_CLASSES


def test_shipped_transcription_runpod_dry_run_reaches_normal_preflight(monkeypatch, tmp_path, sam_offline_lookup):
    import wave

    from astrid.packs.vibecomfy import invocation_preflight
    from astrid.sdk import generation

    audio = tmp_path / "speech.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    calls = []
    original = invocation_preflight.preflight_invocation

    def record(*args, **kwargs):
        calls.append(kwargs.get("phase"))
        return original(*args, **kwargs)

    monkeypatch.setattr(invocation_preflight, "preflight_invocation", record)
    monkeypatch.setattr(generation, "_require_claimed_machine",
                        lambda *args, **kwargs: pytest.fail("dry run required a machine"))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    result = generation.generate.audio(entry="transcription", mode="transcribe",
                                       audio_ref=str(audio), placement="runpod", dry_run=True)
    assert calls == ["dry-run"]
    assert result["status"] != "blocked"
    assert not any(issue["code"] == "missing_node_pack" for issue in result["issues"])


@pytest.mark.parametrize("align", ["check", "trust"])
def test_unprepared_runpod_does_not_assume_shipped_node_is_linked(align, sam_offline_lookup):
    from vibecomfy.comfy_nodes import NODE_CLASS_MAPPINGS
    from astrid.core.generation.preflight import GenerationReadinessError

    entry = entries.find_entry("transcription")
    graph = entries._load_entry_workflow(entry)
    shipped = {node.class_type for node in graph.runtime_nodes().values()} & NODE_CLASS_MAPPINGS.keys()
    assert shipped, "bundled transcription must exercise the actual shipped registry"
    pending = {"engine": "comfyui", "placement": "runpod", "state": "pending_host_readiness"}
    with pytest.raises(GenerationReadinessError) as caught:
        entries.target_schema(entry, graph, None, pending_target=pending, align=align)
    assert shipped <= {issue["details"]["node"] for issue in caught.value.readiness["issues"]
                       if issue["code"] == "missing_node_pack"}


def test_shipped_node_on_attached_target_requires_live_inventory(sam_offline_lookup):
    from types import SimpleNamespace
    from vibecomfy.comfy_nodes import NODE_CLASS_MAPPINGS
    from astrid.core.generation.preflight import GenerationReadinessError

    entry = entries.find_entry("transcription")
    graph = entries._load_entry_workflow(entry)
    active = {node.class_type for node in graph.runtime_nodes().values()}
    shipped = active & NODE_CLASS_MAPPINGS.keys()
    assert shipped
    # No managed-node preparation on an attached server; it must report its
    # own loaded classes even if the controller ships them.
    client = SimpleNamespace(object_info=lambda: {name: {} for name in active - shipped})
    with pytest.raises(GenerationReadinessError) as caught:
        entries.target_schema(entry, graph, client)
    assert shipped <= {issue["details"]["node"] for issue in caught.value.readiness["issues"]
                       if issue["code"] == "missing_node_pack"}


def test_sam_actual_attached_inventory_is_authoritative(network, monkeypatch, sam_api_path, tmp_path):
    from vibecomfy.entries import draft_entry
    from astrid.core.generation.preflight import GenerationReadinessError
    from astrid.sdk.generation import _explicit_workflow_needs

    graph = load_workflow_path(sam_api_path, tmp_path / "canonical").resolved.workflow
    entry = draft_entry(graph, "sam")
    entry["needs"] = _explicit_workflow_needs(entry["needs"])
    info = sam_object_info()
    client, _ = install_target(network, {k: v for k, v in info.items() if k not in SAM_CLASSES})
    with pytest.raises(GenerationReadinessError) as caught:
        entries.target_schema(entry, graph, client)
    assert {issue["details"]["node"] for issue in caught.value.readiness["issues"]
            if issue["code"] == "missing_node_pack"} == SAM_CLASSES
    client, _ = install_target(network, info)
    assert entries.target_schema(entry, graph, client) is not None
