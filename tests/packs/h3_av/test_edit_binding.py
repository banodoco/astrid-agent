"""CPU contracts for the shared imported/native single-operation edit binder."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from PIL import Image

from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.spec import normalize_spec

CASES = json.loads((Path(__file__).parent / "fixtures/edit_binding.json").read_text())
SCHEMA = json.loads((Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/schemas/request.v2.json").read_text())


def _assets(tmp_path: Path) -> dict[str, str]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source fixture; native GPU execution is not under test")
    mask = tmp_path / "mask.png"
    image = Image.new("L", (1024, 576), 0)
    image.paste(255, (8, 12, 40, 36))
    image.save(mask)
    result = {"source.mp4": str(source), "mask.png": str(mask)}
    for name, value in (("look.png", 64), ("other.png", 128)):
        path = tmp_path / name
        Image.new("RGB", (32, 32), (value, value, value)).save(path)
        result[name] = str(path)
    return result


def _compile(raw: dict, tmp_path: Path, assets: dict[str, str], name: str):
    from vibecomfy.cli_loader import load_workflow_any

    preparation = prepare_request(normalize_request(raw), asset_map=assets)
    manifest = compile_preparation(preparation, out_dir=tmp_path / name)
    workflow = load_workflow_any(manifest["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=manifest["workflow_inputs"])
    return manifest, api


def _node(api: dict, class_type: str) -> dict:
    nodes = [row for row in api.values() if row["class_type"] == class_type]
    assert len(nodes) == 1
    return nodes[0]["inputs"]


@pytest.mark.ffmpeg
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_imported_v1_and_v2_use_the_same_native_lowering(case, tmp_path, monkeypatch):
    compiler = importlib.import_module("astrid.packs.h3_av.actions.compile.compile")

    def legacy_route(*args, **kwargs):
        pytest.fail("an edit reached the legacy compiler")

    monkeypatch.setattr(compiler, "_compile_lanpaint", legacy_route)
    assets = _assets(tmp_path)
    assert normalize_request(case["v1"]).canonical == normalize_request(case["v2"]).canonical
    compiled = [_compile(case[f"v{version}"], tmp_path, assets, f"v{version}") for version in (1, 2)]
    for manifest, api in compiled:
        edit = _node(api, "H3V2VGranularFractionalDenoise")
        assert (edit["mode"], edit["crop"], edit["audio_strength"], edit["outside_strength"]) == ("spatial", "disabled", 0, 0)
        assert _node(api, "RandomNoise")["noise_seed"] == 123456789
        assert not any("LanPaint" in row["class_type"] for row in api.values())
        assert api[edit["source_frames"][0]]["inputs"]["start_time"] == 2
        assert api[edit["source_frames"][0]]["inputs"]["frame_load_cap"] == 96
        mask = api[edit["source_mask"][0]]
        assert mask["inputs"]["channel"] == "red"
        if mask["class_type"] == "ImageToMask":
            loader = api[mask["inputs"]["image"][0]]
            assert loader["class_type"] == "VHS_LoadVideoFFmpeg"
            assert loader["inputs"]["frame_load_cap"] == 96
            assert loader["inputs"]["start_time"] == 0
        else:
            assert mask["class_type"] == "LoadImageMask"
        assert manifest["capabilities"]["reference_order"] == ["look.png"]
    # API identities and sealed input contents agree across compatibility import.
    assert compiled[0][1] == compiled[1][1]
    assert compiled[0][0]["managed_assets"]["sha256"] == compiled[1][0]["managed_assets"]["sha256"]


def test_static_mask_is_the_canonical_b2_raster(tmp_path):
    from astrid.packs.h3_av.actions.prepare.static_regions import resolve_static_region

    assets = _assets(tmp_path)
    request = normalize_request(CASES[2]["v2"])
    manifest, api = _compile(CASES[2]["v2"], tmp_path, assets, "compiled")
    expected = resolve_static_region(request.canonical.regions[0], width=1024, height=576,
                                     assets=assets, polarity="black_preserve_white_edit")
    row = next(row for row in manifest["managed_assets"]["manifest"]["assets"] if row["binding"] == "mask_0")
    with zipfile.ZipFile(manifest["managed_assets"]["path"]) as archive:
        assert archive.read(row["member"]) == expected.mask_png
    assert _node(api, "LoadImageMask")["channel"] == "red"


@pytest.mark.ffmpeg
def test_partial_interval_mask_is_black_outside_and_repeatable(tmp_path):
    assets = _assets(tmp_path)
    raw = CASES[3]["v2"]
    first, api = _compile(raw, tmp_path, assets, "first")
    second, _ = _compile(raw, tmp_path, assets, "second")
    assert first["compilation_digest"] == second["compilation_digest"]
    assert first["managed_assets"]["sha256"] == second["managed_assets"]["sha256"]
    path = tmp_path / "first/bounded-mask.mkv"
    with Image.open(assets["mask.png"]) as image:
        expected = image.convert("RGB").tobytes()
    # Decode one hash per actual frame. The shared composition iterator's
    # trim/setpts filter can make ffmpeg infer 25 fps and duplicate 24 fps mask
    # frames; passthrough keeps this compiler contract about frame identity.
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf", "format=gbrp,format=rgb24",
        "-fps_mode", "passthrough", "-an", "-pix_fmt", "rgb24", "-f", "framehash",
        "-hash", "sha256", "-",
    ], check=True, capture_output=True, text=True, timeout=30)
    rows = [line.split(",") for line in decoded.stdout.splitlines() if line and not line.startswith("#")]
    assert len(rows) == 96
    assert all(int(row[-2]) == 1024 * 576 * 3 for row in rows)
    observed = [bytes.fromhex(row[-1].strip()) for row in rows]
    black = hashlib.sha256(bytes(1024 * 576 * 3)).digest()
    white = hashlib.sha256(expected).digest()
    assert observed == [black] * 24 + [white] * 48 + [black] * 24
    assert _node(api, "ImageToMask")["channel"] == "red"


@pytest.mark.parametrize("strength", [0, 0.5, 1])
@pytest.mark.parametrize("guides", [["other", "look"], [], None])
def test_per_operation_strength_seed_and_selected_reference_order(tmp_path, strength, guides):
    raw = copy.deepcopy(CASES[2]["v2"])
    raw["media"].append({"id": "other", "asset": "other.png", "role": "reference", "modality": "image"})
    edit = raw["media"][0]["edit"][0]
    edit.update(strength=strength, seed=0)
    if guides is not None:
        edit["guides"] = guides
    raw["settings"] = {"seed": 99}
    jsonschema.validate(raw, SCHEMA)
    canonical = normalize_request(raw).canonical
    assert canonical.ops[0].strength == strength
    assert canonical.ops[0].seed == 0
    manifest, api = _compile(raw, tmp_path, _assets(tmp_path), "compiled")
    assert _node(api, "H3V2VGranularFractionalDenoise")["inside_strength"] == strength
    assert _node(api, "RandomNoise")["noise_seed"] == 0
    expected = ["other.png", "look.png"] if guides else ([] if guides == [] else ["look.png", "other.png"])
    assert manifest["capabilities"]["reference_order"] == expected
    target = _node(api, "MiniMaxH3ReferenceToVideo")
    ports = [target[key] for key in target if key.startswith("ref_images.")]
    assert len(ports) == len(expected)
    for index, port in enumerate(ports):
        assert api[port[0]]["inputs"]["image"] == manifest["workflow_inputs"][f"reference_{index}"]


@pytest.mark.parametrize("control,value", [
    ("strength", -0.01), ("strength", 1.01), ("strength", float("nan")),
    ("strength", True), ("seed", -1), ("seed", 2**64), ("seed", True),
])
def test_unsupported_operation_controls_fail_closed(control, value):
    raw = copy.deepcopy(CASES[2]["v2"])
    raw["media"][0]["edit"][0][control] = value
    with pytest.raises(H3RequestError):
        normalize_request(raw)
    spec = normalize_request(CASES[2]["v2"]).canonical
    with pytest.raises(H3RequestError):
        normalize_spec(replace(spec, ops=(replace(spec.ops[0], **{control: value}),)))


@pytest.mark.parametrize("mutation", ["audio", "multiple", "polygon", "preserve-control"])
def test_unsupported_edits_fail_before_any_legacy_route(tmp_path, monkeypatch, mutation):
    compiler = importlib.import_module("astrid.packs.h3_av.actions.compile.compile")
    monkeypatch.setattr(compiler, "_compile_lanpaint", lambda *a, **kw: pytest.fail("legacy route reached"))
    raw = copy.deepcopy(CASES[2]["v2"])
    edits = raw["media"][0]["edit"]
    if mutation == "audio":
        edits.append({"stream": "audio", "during": [0, 4]})
    elif mutation == "multiple":
        edits[0]["during"] = [0, 2]
        edits.append({**edits[0], "during": [2, 4], "seed": 17})
    elif mutation == "polygon":
        edits[0]["mask"] = {"polygon": [[0, 0], [20, 0], [20, 20]]}
    else:
        edits[0].update(action="preserve", seed=1)
    with pytest.raises((H3RequestError, CompilationError)):
        _compile(raw, tmp_path, _assets(tmp_path), "rejected")


@pytest.mark.parametrize("control,value", [("strength", 0.5), ("seed", 17)])
@pytest.mark.parametrize("route", ["character_swap", "continuation"])
def test_per_operation_controls_fail_closed_on_other_native_routes(tmp_path, control, value, route):
    if route == "character_swap":
        from tests.packs.h3_av.test_character_swap import _files, _preparation, _request

        raw = _request(source=True)
        raw["media"][0]["edit"][0][control] = value
        preparation = _preparation(raw, _files(tmp_path, source=True))
        message = "character_swap_v1 does not bind per-operation strength or seed"
    else:
        raw = copy.deepcopy(CASES[2]["v2"])
        raw.update(continuation=True, duration=5)
        raw["media"][0]["edit"][0][control] = value
        preparation = prepare_request(normalize_request(raw), asset_map=_assets(tmp_path))
        message = "native-v2 continuation has no per-operation strength or seed binding"
    with pytest.raises(CompilationError, match=message):
        compile_preparation(preparation, out_dir=tmp_path / "rejected")
