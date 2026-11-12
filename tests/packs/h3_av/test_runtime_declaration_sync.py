from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml
from PIL import Image

from astrid.core.generation.engine_recipe import load_recipe
from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.generation import build_generation_workflow
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.workflows.native_h3_continuation.workflow import READY_METADATA as CONTINUATION
from astrid.packs.h3_av.workflows.native_h3_continuation_refs.workflow import READY_METADATA as CONTINUATION_REFS


PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"


def _recipe_runtime() -> dict[str, str]:
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    source = next(row for row in recipe.sources if row.name == "comfyui")
    # Version read from comfyui_version.py at the immutable source commit.
    assert source.pin.revision == "6bfaacc67c2103481e5f0c84d75257cd0581d86a"
    observed_version = "0.36.0"
    environment = next(row for row in recipe.environments if row.name == recipe.primary_environment)
    lock_ref = environment.requirements.packaged_lock
    assert lock_ref is not None
    lock_path = PACK.parents[1] / lock_ref.path
    lock_bytes = lock_path.read_bytes()
    assert hashlib.sha256(lock_bytes).hexdigest() == lock_ref.sha256
    lock = lock_bytes.decode("utf-8")
    packages = {
        name: "==" + re.search(rf"^{re.escape(name)}==([^\s\\]+)", lock, re.MULTILINE).group(1)
        for name in ("comfy-kitchen", "comfy-aimdo")
    }
    torch = next(row.version for row in environment.final_wheels if row.name == "torch")
    return {
        "comfy_commit": source.pin.revision,
        "comfy_version": f"=={observed_version}",
        "packages": {"torch": f"=={torch}", **packages},
    }


def _assert_runtime(actual: dict, expected: dict[str, str]) -> None:
    assert actual["comfy_commit"] == expected["comfy_commit"]
    assert actual["comfy_version"] == expected["comfy_version"]
    assert actual["packages"] == expected["packages"]


def _compile_v2_context(tmp_path: Path, *, with_reference: bool):
    tmp_path.mkdir(parents=True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"CPU structural fixture; compilation only archives source bytes")
    media = [{
        "id": "source", "asset": "source.mp4", "role": "timeline", "modality": "video",
        "at": {"frame": 0}, "range": [0, 1],
    }]
    asset_map = {"source.mp4": str(source)}
    if with_reference:
        reference = tmp_path / "reference.png"
        Image.new("RGB", (16, 16), color=(32, 64, 128)).save(reference)
        media.append({"id": "look", "asset": "reference.png", "role": "reference", "modality": "image"})
        asset_map["reference.png"] = str(reference)
    request = normalize_request({
        "version": 2, "prompt": "Continue the source.", "duration": 2,
        "continuation": True, "media": media, "settings": {},
    })
    prepared = prepare_request(request, asset_map=asset_map)
    return compile_preparation(prepared, out_dir=tmp_path / "compiled")


def test_active_h3_runtime_declarations_match_the_shared_recipe_and_lock(tmp_path: Path) -> None:
    expected = _recipe_runtime()

    # The generated native-v1/source-free graph is the active built form.
    generated = build_generation_workflow(
        references=["reference.png"], prompt="A scene.", frames=24,
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1,
    )
    _assert_runtime(generated.metadata["requirements"]["runtime"], expected)
    reference = tmp_path / "generated/reference.png"
    reference.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), color=(32, 64, 128)).save(reference)
    v1_request = normalize_request({
        "version": 1, "operation": "generate", "output": {"duration": 1},
        "content": {"prompt": "A scene."}, "changes": {"video": [], "audio": []},
        "references": [{"asset": "reference.png", "purpose": "appearance"}],
        "overrides": {"steps": 8, "seed": 1},
    })
    v1_preparation = prepare_request(v1_request, asset_map={"reference.png": str(reference)})
    v1_compiled = compile_preparation(v1_preparation, out_dir=tmp_path / "generated/compiled")
    assert v1_compiled["workflow"]["workflow.py"]["path"].endswith("workflow-bundle/workflow.py")

    # Native-v1 continuation copies its primary scaffold; native-v2 chooses the
    # reference scaffold only when references exist. These compile paths prove
    # the metadata consumed by the engine, rather than only checking source text.
    no_reference = _compile_v2_context(tmp_path / "without-reference", with_reference=False)
    with_reference = _compile_v2_context(tmp_path / "with-reference", with_reference=True)
    assert no_reference["workflow"]["workflow.py"]["path"].endswith(
        "workflow-bundle/workflow.py"
    )
    assert with_reference["workflow"]["workflow.py"]["path"].endswith(
        "workflow-bundle/workflow.py"
    )
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle

    selected_generated = load_bundle(
        v1_compiled["workflow"]["workflow.py"]["path"], trust=Provenance.AGENT_AUTHORED
    )
    _assert_runtime(selected_generated.workflow.metadata["requirements"]["runtime"], expected)

    for compiled, expected_metadata in (
        (no_reference, CONTINUATION),
        (with_reference, CONTINUATION_REFS),
    ):
        selected = load_bundle(
            compiled["workflow"]["workflow.py"]["path"], trust=Provenance.USER_CONFIRMED
        )
        _assert_runtime(selected.workflow.metadata["requirements"]["runtime"], expected)
        _assert_runtime(expected_metadata["requirements"]["runtime"], expected)

    profile = yaml.safe_load((PACK / "profiles/native-v1.yaml").read_text(encoding="utf-8"))
    assert profile["runtime"]["comfy_version"] == expected["comfy_version"]
    for package, version in expected["packages"].items():
        assert profile["runtime"][package] == version

    contribution = yaml.safe_load((PACK / "extensions/vibecomfy.workflows/h3.yaml").read_text())
    assert contribution["entries"][0]["bundle"] == "native_h3_continuation"
