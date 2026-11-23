"""P56 WS-7a: the engine transports recipes without installing them."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.core.generation.entries import engine_table

NODE = {"repo": "https://example.test/pack.git", "commit": "a" * 40,
        "requirements_txt": "requirements.txt", "pip_no_deps": ["z==2", "a==1"]}


def _workflow(node):
    return SimpleNamespace(metadata={"requirements": {"custom_node_refs": [{
        "slug": "pack", "source": "git", "url": node["repo"], **{k: v for k, v in node.items() if k != "repo"},
    }]}})


def test_recipe_fields_survive_real_engine_manifest_and_validation():
    engine = engine_table()["comfyui"]
    workflow = _workflow(copy.deepcopy(NODE))
    manifest = engine.workflow_requirements([workflow])
    assert manifest["extra"] == {"nodes": [NODE]}
    engine.validate_extra(manifest["extra"])
    manifest["extra"]["nodes"][0]["pip_no_deps"].append("extra")
    assert workflow.metadata["requirements"]["custom_node_refs"][0]["pip_no_deps"] == ["z==2", "a==1"]


def test_identity_only_refs_keep_the_previous_shape():
    engine = engine_table()["comfyui"]
    node = {key: NODE[key] for key in ("repo", "commit")}
    assert engine.workflow_requirements([_workflow(node)])["extra"] == {"nodes": [node]}


def test_duplicate_legacy_metadata_does_not_erase_a_declared_recipe():
    engine = engine_table()["comfyui"]
    workflow = _workflow(NODE)
    workflow.metadata["custom_node_packs"] = {"pack": {"repository": NODE["repo"], "revision": NODE["commit"]}}
    assert engine.workflow_requirements([workflow])["extra"] == {"nodes": [NODE]}


@pytest.mark.parametrize("change", [
    {"requirements_txt": "elsewhere.txt"}, {"requirements_txt": None},
    {"pip_no_deps": ["z==3"]}, {"commit": "b" * 40},
])
def test_two_explicit_conflicting_recipe_or_identity_rows_are_refused(change):
    engine = engine_table()["comfyui"]
    with pytest.raises(ValueError, match="conflict"):
        engine.workflow_requirements([_workflow(NODE), _workflow({**NODE, **change})])


@pytest.mark.parametrize("change", [
    {"requirements_txt": "../other"}, {"requirements_txt": True}, {"pip_no_deps": ["--help"]},
    {"pip_no_deps": "pkg"}, {"pip_no_deps": [True]}, {"install_py": True},
    {"commit": None}, {"repo": ""},
])
def test_manifest_rejects_malformed_or_unrecognized_fields(change):
    with pytest.raises(ValueError):
        engine_table()["comfyui"].validate_extra({"nodes": [{**NODE, **change}]})
