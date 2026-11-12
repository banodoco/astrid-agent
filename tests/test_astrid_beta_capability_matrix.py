from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation import entries
from astrid.core.generation.preflight import GenerationReadinessError, require_generation_target_readiness


def test_beta_matrix_covers_every_discovered_capability_and_declaration():
    import yaml

    host = GenericPackHost(pack_roots=[Path("astrid/packs")])
    records = host.discover()
    # Rows for packs that moved to poms_packs name their pack and are absent
    # until the user installs it; every other row must be discovered.
    external_pack_rows = {
        capability_id
        for capability_id, row in host.matrix.items()
        if row.get("external_pack")
    }
    declaration = yaml.safe_load(
        Path("astrid/packs/h3_av/extensions/vibecomfy.workflows/h3.yaml").read_text(encoding="utf-8")
    )["entries"][0]
    h3 = entries.find_entry("h3")
    assert h3 is not None
    assert h3["contribution"] == "h3_av.h3"
    assert h3["id"] == declaration["id"] == "h3"
    assert h3["kind"] == declaration["kind"] == "video"
    assert h3["modes"] == declaration["modes"] == ["i2v", "v2v", "video-edit"]
    assert h3["inputs"] == declaration["inputs"]
    assert [(port["name"], port["type"], port["required"]) for port in h3["inputs"]] == [
        ("request", "file", True), ("asset_map", "file_map", True),
    ]
    # Every discovered capability must have a row in
    # config/astrid-beta-capabilities.json; no discovered id is exempted.
    assert {record.id for record in records} == (
        set(host.matrix) - external_pack_rows
    )
    assert host.stale_matrix_entries == ()
    assert {record.matrix["disposition"] for record in records} <= {"required", "optional", "unsupported", "retired"}
    for record in records:
        assert record.matrix["evidence_reason"]
        assert record.matrix["adapter_family"] == record.adapter.family
        assert isinstance(record.resource_keys, tuple)
        assert record.resource_keys
        assert record.capability_digest and record.source_digest
        assert isinstance(record.manifest()["inputs"], list)
        assert isinstance(record.manifest()["outputs"], list)


def test_music_duck_matrix_declares_its_runtime_dependencies():
    matrix = json.loads(Path("config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    row = next(item for item in matrix["capabilities"] if item["id"] == "music.duck")

    assert row["required_binaries"] == ["ffmpeg", "ffprobe"]
    assert row["required_packages"] == ["numpy"]


def test_beta_reference_family_preflight_is_truthful_on_this_machine():
    host = GenericPackHost(pack_roots=[Path("astrid/packs")])
    host.discover()
    host.preflight()

    cpu = host.capabilities["rendering.timeline_export_plan"]
    assert cpu.adapter.family == "cpu"
    assert cpu.ready

    h3 = entries.find_entry("h3")
    assert h3 is not None
    mode = h3["default_mode"]
    prerequisite = SimpleNamespace(id=h3["id"], modes={mode: SimpleNamespace(
        backends={"local": SimpleNamespace(template=str(h3["bundle"]))},
    )})
    with pytest.raises(GenerationReadinessError) as caught:
        require_generation_target_readiness(prerequisite, mode, None)
    assert caught.value.readiness["entry"] == {"id": "h3", "engine": "comfyui", "mode": mode}
    assert caught.value.readiness["status"] == "blocked"
    assert caught.value.readiness["issues"][0]["code"] == "engine_not_found"
    assert "no managed engine is installed or registered" in caught.value.readiness["issues"][0]["message"]

    provider = host.capabilities["generation.generate_image_openai"]
    assert provider.adapter.family == "provider"
    assert provider.ready

    render = host.capabilities["rendering.render"]
    assert render.adapter.family == "render"
    # Remotion follows the renderer a task selects, so it reports but never gates readiness.
    assert render.adapter.remotion == "selected"
    assert render.preflight["remotion"]["gates"] == "tasks that render with rendering.remotion"
    if not render.ready:
        assert render.preflight["binaries"]["missing"]

    # These helpers are offline pack executors.  They share the rendering
    # namespace but do not inherit the final compositor's Remotion dependency.
    for capability_id in (
        "rendering.html_canvas_effect",
        "rendering.sprite_sheet",
        "rendering.timeline_storyboard",
        "rendering.timeline_visualize",
    ):
        helper = host.capabilities[capability_id]
        assert helper.adapter.family == "render"
        assert helper.adapter.remotion == "never"
        assert "remotion" not in helper.preflight
        if capability_id == "rendering.sprite_sheet" and not helper.ready:
            assert helper.preflight["binaries"]["missing"] == ["ffmpeg"]
            continue  # without ffmpeg this helper reports truthfully that it is not ready
        assert helper.ready

    local = host.capabilities["vibecomfy.run"]
    assert local.adapter.family == "local_generation"
    if not local.ready:
        assert local.preflight["packages"]["missing"]


def test_pack_and_matrix_agree_on_render_remotion_requirement():
    import yaml

    pack = yaml.safe_load(Path("astrid/packs/rendering/pack.yaml").read_text(encoding="utf-8"))
    declared = pack["actions"]["render"]["metadata"]["requires_remotion"]
    matrix = json.loads(Path("config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    entry = next(item for item in matrix["capabilities"] if item["id"] == "rendering.render")

    assert declared == entry["requires_remotion"] == "selected"
    # Node is validated by the Remotion check, not by a binary the render needs always.
    assert "node" not in entry["required_binaries"]


def test_bounded_typed_media_is_optional_until_gpu_output_proof():
    host = GenericPackHost(pack_roots=[Path("astrid/packs")])
    host.discover()
    host.preflight()

    for capability_id in ("vibecomfy.video_enhance", "vibecomfy.character_animation"):
        record = host.capabilities[capability_id]
        assert record.matrix["disposition"] == "optional"
        assert "GPU" in record.matrix["evidence_reason"]
        assert record.adapter.family == "local_generation"


def test_provider_ledger_rows_are_networked_and_credential_dispositions_match():
    """B9.4: provider declarations cannot silently become offline executors."""
    # Source reconciliation requires the checkout's config path; the default
    # packaged resource does not carry a checkout-relative source inventory.
    host = GenericPackHost(
        pack_roots=[Path("astrid/packs")],
        capability_matrix=Path("config/astrid-beta-capabilities.json"),
    )
    records = host.discover()
    provider_rows = [record for record in records if record.adapter.family == "provider"]
    assert provider_rows

    by_credential = {
        str(row["credential"]): set(row["capabilities"])
        for row in host.ledger["sources"]["providers"]
    }
    for record in provider_rows:
        assert record.definition.isolation.network is True
        required_env = record.matrix.get("required_env") or ()
        for credential in required_env:
            assert record.id in by_credential[str(credential)]
        # A source manifest's provider metadata and the frozen matrix must
        # agree on the family; this catches a provider accidentally advertised
        # as a CPU/local capability after a manifest edit.
        assert record.matrix["adapter_family"] == "provider"


def test_manifest_secrets_report_missing_without_gating_readiness(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "missing.env"))
    for key in ("GIPHY_API_KEY", "FAL_KEY", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    host = GenericPackHost(pack_roots=[Path("astrid/packs")])
    host.discover()
    host.preflight()
    # media.gif_search, media.speech_repair_lavasr and fal.h3_video moved to
    # poms_packs with their packs; their checks belong to those packs now.
    for capability_id in ("generation.generate_image", "generation.generate_video"):
        record = host.capabilities[capability_id]
        assert record.preflight["credentials"]["missing"] == ["FAL_KEY"]
        assert record.ready is True
    loras = host.capabilities["training.search_loras"]
    assert loras.preflight["credentials"] == {
        "ok": True, "missing": ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"],
    }
    assert loras.ready is True


def test_video_understanding_matrix_matches_manifest_credentials():
    # The understanding pack moved to poms_packs, so only its matrix rows are
    # in this tree; the manifest side is checked by the pack's own tests.
    host = GenericPackHost(pack_roots=[Path("astrid/packs")])
    host.discover()
    for capability_id in ("understanding.video_understand", "understanding.scene_describe"):
        assert capability_id not in host.capabilities
        row = host.matrix[capability_id]
        assert row["required_env"] == ["GEMINI_API_KEY"]
        assert row["external_pack"] == "understanding"
        assert row["disposition"] == "optional"
