"""H3 request overrides are applied to the graph, refused loudly, or recorded: never silently dropped."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.generation import PIKA_REF2VA_FILENAME
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.stages import settings_record
from vibecomfy.security.provenance import Provenance
from vibecomfy.workflow_bundle import load_bundle

PIKA = {"steps": 8, "seed": 42, "turbo_lora": "pika_flex_8step", "guidance": 1.0, "sampler": "res_multistep",
        "scheduler": "simple", "cfg": 1.0, "video_shift": 12.0, "audio_shift": 3.0}


def _compile(tmp_path: Path, overrides: dict):
    png = tmp_path / "ref.png"
    Image.new("RGB", (64, 64), "red").save(png)
    raw = {"version": 1, "operation": "generate", "output": {"duration": 2.5},
           "content": {"prompt": "A man listens."}, "changes": {"video": [], "audio": []},
           "references": [{"asset": "img", "purpose": "appearance"}], "overrides": overrides}
    compiled = compile_preparation(prepare_request(normalize_request(raw), asset_map={"img": str(png)}),
                                   out_dir=tmp_path / "out")
    workflow = load_bundle(tmp_path / "out" / "generation-bundle" / "workflow.py",
                           trust=Provenance.USER_CONFIRMED).workflow
    graph = workflow.compile("api")
    return compiled, {v["class_type"]: v["inputs"] for v in graph.values()}, graph


def test_overrides_reach_the_graph_and_the_record(tmp_path):
    compiled, nodes, _ = _compile(tmp_path, {"steps": 6, "seed": 42, "sampler": "euler", "scheduler": "beta",
                                             "guidance": 0.7, "video_shift": 10.0, "audio_shift": 2.0})
    assert nodes["BasicScheduler"]["steps"] == 6 and nodes["BasicScheduler"]["scheduler"] == "beta"
    assert nodes["KSamplerSelect"]["sampler_name"] == "euler"
    assert nodes["LoraLoaderModelOnly"]["strength_model"] == 0.7
    assert nodes["LoraLoaderModelOnly"]["lora_name"] == PIKA_REF2VA_FILENAME
    assert (nodes["MiniMaxH3SigmaShift"]["shift_video"], nodes["MiniMaxH3SigmaShift"]["shift_audio"]) == (10.0, 2.0)
    record = settings_record(compiled)
    assert record["effective_settings"]["steps"] == 6
    assert set(record["differs_from_defaults"]) == {
        "steps", "seed", "sampler", "scheduler", "turbo.strength", "video_shift", "audio_shift"}
    assert record["differs_from_defaults"]["steps"] == {"value": 6, "default": 8}


def test_requests_differing_only_in_steps_have_different_hashes(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    a = settings_record(_compile(tmp_path / "a", {"steps": 8, "seed": 42})[0])
    b = settings_record(_compile(tmp_path / "b", {"steps": 6, "seed": 42})[0])
    assert a["workflow_inputs_sha256"] != b["workflow_inputs_sha256"]
    assert (a["effective_settings"]["steps"], b["effective_settings"]["steps"]) == (8, 6)


def test_pika_overrides_equal_the_defaults_and_say_so(tmp_path):
    compiled, _, _ = _compile(tmp_path, PIKA)
    record = settings_record(compiled)
    assert set(record["differs_from_defaults"]) == {"seed"}
    assert record["defaults"]["turbo.lora"] == "pika_flex_8step"


def test_turbo_lora_none_removes_the_loader(tmp_path):
    compiled, nodes, graph = _compile(tmp_path, {"seed": 42, "turbo_lora": "none"})
    assert "LoraLoaderModelOnly" not in nodes
    assert not any(PIKA_REF2VA_FILENAME in str(v["inputs"].values()) for v in graph.values())
    assert settings_record(compiled)["effective_settings"]["turbo.enabled"] is False
    assert "guidance" not in compiled["workflow_inputs"]


@pytest.mark.parametrize("overrides,match", [
    ({"turbo_lora": "lightx2v_8step"}, "no matching variant"),
    ({"turbo_lora": "absent"}, "unknown settings.turbo.lora"),
    ({"cfg": 2.0}, "settings.cfg"),
])
def test_unappliable_overrides_are_refused(tmp_path, overrides, match):
    with pytest.raises(H3RequestError, match=match):
        _compile(tmp_path, overrides)


def _hooks():
    path = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py"
    spec = importlib.util.spec_from_file_location("h3_hooks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plain_inputs_beside_a_request_document_are_refused_not_ignored():
    refuse = _hooks()._refuse_ignored_inputs
    refuse({"request": "r.json", "seed": 42}, {}, {"seed": 42}, plain=False)
    refuse({"seed": 7}, {}, {"seed": 1}, plain=True)
    with pytest.raises(ValueError, match="seed=7.*differs"):
        refuse({"request": "r.json", "seed": 7}, {}, {"seed": 42}, plain=False)
    with pytest.raises(ValueError, match="steps=4.*differs"):
        refuse({"request": "r.json", "steps": 4}, {}, {"seed": 42}, plain=False)
    with pytest.raises(ValueError, match="'prompt' is ignored"):
        refuse({"request": "r.json", "prompt": "x"}, {}, {}, plain=False)
