"""Keep the Agent-owned H3 refs declaration and constructor in parity."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from vibecomfy.security.provenance import Provenance
from vibecomfy.workflow_bundle import _semantic_edges, emit_bundle, load_bundle


PAIR = (Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/workflows"
        / "native_h3_continuation_refs/workflow.py")
REF2V_LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
FL2V_LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"


def test_declared_edges_match_constructor_output() -> None:
    # Use the canonical validator for every declared edge, including native
    # socket rosters. Do not reconstruct executable edges from presentation.
    bundle = load_bundle(PAIR, trust=Provenance.USER_CONFIRMED)
    # Reuse the canonical semantic projection so a missing declaration is
    # caught as well as a declared connection missing from Python.
    declared_edges = {
        tuple(link["edge_ref"][field] for field in (
            "scope_path", "from_uid", "from_port", "to_uid", "to_port",
        ))
        for link in bundle.ui_sidecar["presentation"]["links"]
        if "edge_ref" in link
    }
    assert declared_edges == _semantic_edges(bundle.workflow)
    api = bundle.workflow.compile("api")
    assert api["100"]["class_type"] == "MiniMaxH3CropTo32"
    assert api["972"]["class_type"] == "MiniMaxH3StartCanvasSelector"

    source = json.loads(PAIR.with_name("source.json").read_text())
    selector = next(node for node in source["nodes"] if node["id"] == 972)
    for link_id, source_slot, target_slot, field in (
        (166, 1, 3, "source_width"), (167, 2, 4, "source_height"),
    ):
        assert [link_id, 100, source_slot, 972, target_slot, "INT"] in source["links"]
        assert selector["inputs"][target_slot]["name"] == field
        declared = next(link for link in bundle.ui_sidecar["presentation"]["links"]
                        if link["id"] == link_id)
        assert declared["edge_ref"] == {
            "scope_path": "", "from_uid": "100", "from_port": source_slot,
            "to_uid": "972", "to_port": target_slot,
        }
        assert api["972"]["inputs"][field] == ["100", source_slot]


@pytest.mark.parametrize("start", ["Existing Video", "T2V", "I2V / Custom Keyframes"])
def test_start_modes_preserve_canvas_edges_through_canonical_roundtrip(
    tmp_path: Path, start: str,
) -> None:
    bundle = load_bundle(PAIR, trust=Provenance.USER_CONFIRMED)
    workflow = bundle.workflow.copy()
    # The upstream frontend mirrors Start into its cache-isolated parameter.
    for node in workflow.nodes.values():
        if node.class_type in {"MiniMaxH3AVExtensionController", "MiniMaxH3AVStartModeParam"}:
            node.inputs["start"] = start
    api = workflow.compile("api")
    assert api["935"]["inputs"]["lora_name"] == REF2V_LORA
    assert api["935"]["inputs"]["lora_name"] != FL2V_LORA
    canvas = api["972"]["inputs"]
    assert canvas["source_width"] == ["100", 1]
    assert canvas["source_height"] == ["100", 2]
    assert canvas["generated_width"] == ["1024", 0]
    assert canvas["generated_height"] == ["1024", 1]
    mode_node, mode_slot = canvas["start_mode"]
    assert mode_slot == 0
    assert api[mode_node]["class_type"] == "MiniMaxH3AVStartModeParam"
    assert api[mode_node]["inputs"]["start"] == start

    destination = tmp_path / "workflow.py"
    shutil.copyfile(PAIR.with_suffix(".vibe.json"), destination.with_suffix(".vibe.json"))
    published = emit_bundle(workflow, destination, provenance=Provenance.AGENT_AUTHORED)
    reopened = load_bundle(destination, trust=Provenance.USER_CONFIRMED)
    assert published.semantic_digest == reopened.semantic_digest == workflow.semantic_digest()
    assert published.ui_digest == reopened.ui_digest == bundle.ui_digest
    assert reopened.workflow.compile("api") == api


def test_ref2va_uses_ref2v_lora_in_source_and_canonical_pair(tmp_path: Path) -> None:
    bundle = load_bundle(PAIR, trust=Provenance.USER_CONFIRMED)
    api = bundle.workflow.compile("api")
    assert api["935"]["class_type"] == "LoraLoaderModelOnly"
    assert api["935"]["inputs"]["lora_name"] == REF2V_LORA
    assert api["935"]["inputs"]["lora_name"] != FL2V_LORA

    source = json.loads(PAIR.with_name("source.json").read_text())
    lora = next(node for node in source["nodes"] if node["id"] == 935)
    assert lora["widgets_values_named"]["lora_name"] == REF2V_LORA
    assert lora["widgets_values"][0] == REF2V_LORA

    destination = tmp_path / "workflow.py"
    shutil.copyfile(PAIR.with_suffix(".vibe.json"), destination.with_suffix(".vibe.json"))
    emitted = emit_bundle(bundle.workflow, destination, provenance=Provenance.AGENT_AUTHORED)
    reopened = load_bundle(destination, trust=Provenance.USER_CONFIRMED)
    assert emitted.workflow.compile("api")["935"]["inputs"]["lora_name"] == REF2V_LORA
    assert reopened.workflow.compile("api")["935"]["inputs"]["lora_name"] == REF2V_LORA
    assert emitted.semantic_digest == reopened.semantic_digest == bundle.semantic_digest
