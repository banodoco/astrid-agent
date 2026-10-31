from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from astrid.core.generation.engine_recipe import engine_source_revision, load_recipe
from astrid.packs.h3_av.workflows.native_h3_continuation.workflow import READY_METADATA as CONTINUATION
from astrid.packs.h3_av.workflows.native_h3_continuation_refs.workflow import READY_METADATA as REFERENCES


ENGINE = "vibecomfy-h3"
PLATFORM = "linux-x86_64-cu130"
ROOT = Path(__file__).resolve().parents[3]


def _locked_versions() -> tuple[dict[str, str], str, str]:
    recipe = load_recipe(ENGINE, PLATFORM)
    environment = next(item for item in recipe.environments if item.name == recipe.primary_environment)
    lock_ref = environment.requirements.packaged_lock
    assert lock_ref is not None
    lock_path = ROOT / "astrid" / lock_ref.path
    lock_bytes = lock_path.read_bytes()
    assert hashlib.sha256(lock_bytes).hexdigest() == lock_ref.sha256
    lock = lock_bytes.decode("utf-8")

    comfy = re.search(r"^# ComfyUI v([^\s:]+): ([0-9a-f]{40})/", lock, re.MULTILINE)
    assert comfy is not None
    versions = {
        name: re.search(rf"^{re.escape(name)}==([^\s\\]+)", lock, re.MULTILINE).group(1)
        for name in ("comfy-aimdo", "comfy-kitchen")
    }
    torch = next(item.version for item in environment.final_wheels if item.name == "torch")
    versions["torch"] = torch
    return versions, comfy.group(1), comfy.group(2)


def test_runnable_h3_workflow_and_profile_runtime_match_pinned_recipe() -> None:
    recipe = load_recipe(ENGINE, PLATFORM)
    versions, comfy_version, comfy_commit = _locked_versions()
    assert comfy_commit == engine_source_revision(ENGINE, PLATFORM, "comfyui")

    source_by_name = {source.name: source for source in recipe.sources}
    node_source_names = {
        "ComfyUI-H3-Motion-Context-MultiRef": "h3-motion-context",
        "ComfyUI-VideoHelperSuite": "videohelpersuite",
    }
    for metadata in (CONTINUATION, REFERENCES):
        runtime = metadata["requirements"]["runtime"]
        assert runtime["comfy_commit"] == comfy_commit
        assert runtime["comfy_version"] == f"=={comfy_version}"
        assert runtime["packages"] == {name: f"=={version}" for name, version in versions.items()}
        for node_pack, source_name in node_source_names.items():
            source = source_by_name[source_name]
            assert metadata["custom_node_packs"][node_pack]["commit"] == engine_source_revision(
                ENGINE, PLATFORM, source_name
            )
            assert metadata["custom_node_packs"][node_pack]["url"] == source.pin.repository

    profile = yaml.safe_load((ROOT / "astrid/packs/h3_av/profiles/native-v1.yaml").read_text())
    profile_runtime = profile["runtime"]
    assert profile_runtime["comfy_version"] == f"=={comfy_version}"
    assert profile_runtime["torch"] == f"=={versions['torch']}"
    assert profile_runtime["comfy-kitchen"] == f"=={versions['comfy-kitchen']}"
    assert profile_runtime["comfy-aimdo"] == f"=={versions['comfy-aimdo']}"
