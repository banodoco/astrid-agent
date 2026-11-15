"""On an attached ComfyUI, no readiness, preflight or engine message suggests align --apply.

Missing core nodes say to update ComfyUI; missing custom nodes name their pack (and repository
when the lock declares one) and say the person installs and restarts it. Managed messages keep align.
"""
from __future__ import annotations

import json

import pytest
from vibecomfy.node_packs import CustomNodePack

from astrid.core.generation import preflight
from astrid.core.generation import asset_gaps
from astrid.core.generation.asset_gaps import asset_readiness
from astrid.core.generation.backends.comfy_target import (
    ComfyTarget, ComfyTargetClient, _capture_model_resolution,
)
from astrid.core.generation.preflight import GenerationReadinessError, select_generation_target
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.packs.vibecomfy import production_engine
from tests.core.generation.backends.test_comfy_model_resolution import target_info
from tests.core.generation.test_missing_model_attached import FOLDERS, _fake_server
from tests.packs.vibecomfy.test_local_server_run import _workflow, comfy  # noqa: F401 - fixture and workflow

URL = "http://127.0.0.1:8188"
CUSTOM = CustomNodePack(
    name="ComfyUI-Fake", repo="https://github.com/example/ComfyUI-Fake.git",
    classes=frozenset({"AstridFakeNode"}),
)


@pytest.fixture
def known_packs(monkeypatch):
    """Only the fake pack is locked; every other class is a core or unknown node."""
    def resolve(class_types, *, lockfile_path=None):
        return [CUSTOM] if set(class_types) & CUSTOM.classes else []

    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs", resolve)


def _assert_no_align(readiness):
    text = json.dumps(readiness)
    assert "align" not in text
    assert "argv" not in text
    assert "astrid generate" not in text
    assert readiness["fixes"], "attached readiness must still carry a fix"
    assert all(fix["scope"] == "attached_target" for fix in readiness["fixes"])


def test_attached_core_node_says_update_comfyui(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", nodes=["CLIPLoader"])
    assert readiness["issues"][0]["message"] == (
        "Your ComfyUI does not have CLIPLoader, which is part of ComfyUI itself; update ComfyUI, then retry."
    )
    _assert_no_align(readiness)


def test_attached_custom_node_names_pack_repository_and_manual_install(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", nodes=["AstridFakeNode"])
    message = readiness["issues"][0]["message"]
    assert "AstridFakeNode" in message
    assert "ComfyUI-Fake" in message
    assert "https://github.com/example/ComfyUI-Fake.git" in message
    assert "custom_nodes" in message
    assert "restart ComfyUI yourself" in message
    _assert_no_align(readiness)


def test_attached_missing_model_names_file_without_align(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", models=["x.safetensors"])
    assert readiness["issues"][0]["code"] == "missing_model"
    assert "x.safetensors" in readiness["issues"][0]["message"]
    _assert_no_align(readiness)


def test_attached_missing_node_in_model_resolution_has_no_align(monkeypatch):
    info = target_info("t2i")
    info.pop("VAELoader")
    _fake_server(monkeypatch, info, FOLDERS)
    client = ComfyTargetClient(ComfyTarget(URL, "attached"))
    with pytest.raises(GenerationReadinessError) as caught:
        _capture_model_resolution(client, ModelRegistry.load_default(), "z-image", "t2i")
    readiness = caught.value.readiness
    assert readiness["issues"][0]["code"] == "missing_node_pack"
    assert readiness["issues"][0]["message"].startswith("Your ComfyUI does not have VAELoader")
    _assert_no_align(readiness)


def test_select_generation_target_keeps_attached_node_readiness_without_align(monkeypatch):
    readiness = {
        "entry": {"id": "z-image", "engine": "comfyui"}, "target": None, "status": "blocked",
        "issues": [{"code": "missing_node_pack", "severity": "error",
                    "message": "Your ComfyUI does not have CLIPLoader, which is part of ComfyUI itself; "
                               "update ComfyUI, then retry.",
                    "details": {"node": "CLIPLoader"}}],
        "fixes": [{"scope": "attached_target", "details": {"action": "update ComfyUI, then retry."}}],
    }

    def refuse(*_args, **_kwargs):
        raise GenerationReadinessError(readiness)

    monkeypatch.setattr(preflight, "_select_generation_target", refuse)
    with pytest.raises(GenerationReadinessError) as caught:
        select_generation_target(URL, entry_id="z-image", mode="t2i")
    _assert_no_align(caught.value.readiness)


def test_managed_missing_node_keeps_align_readiness():
    readiness = asset_readiness("z-image", nodes=["CLIPLoader"])
    assert "align --apply" in readiness["issues"][0]["message"]
    assert readiness["fixes"][0]["argv"][:4] == ["astrid", "generate", "align", "--apply"]


def test_attached_live_preflight_missing_node_names_no_align(comfy, tmp_path):
    workflow = _workflow()
    workflow["nodes"][0].update(
        type="AstridDefinitelyMissingNode",
        properties={"Node name for S&R": "AstridDefinitelyMissingNode"},
        widgets_values=[],
    )
    comfy.workflow.write_text(json.dumps(workflow))
    with pytest.raises(production_engine.ProductionEngineError,
                       match=r"missing nodes: AstridDefinitelyMissingNode.*Missing nodes and models") as caught:
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    message = str(caught.value)
    assert "align" not in message
    assert "astrid generate" not in message
    assert "update ComfyUI" in message
    assert not comfy.prompts


PINNED = "0123456789abcdef0123456789abcdef01234567"
LOCK_TEXT = f"""
[nodepacks.ComfyUI-Fake]
name = "ComfyUI-Fake"
url = "https://github.com/example/ComfyUI-Fake.git"
commit = "{PINNED}"
git_commit_sha = "{PINNED}"
class_set = ["AstridFakeNode", "AstridFakeNodeTwo"]
"""
PINNED_FAKE = CustomNodePack(
    name="ComfyUI-Fake", repo="https://github.com/example/ComfyUI-Fake.git",
    classes=frozenset({"AstridFakeNode", "AstridFakeNodeTwo"}),
)


@pytest.fixture
def fake_pack_with_lock(monkeypatch, tmp_path):
    """The fake pack, locked at PINNED; the lock file is real so the commit comes from it."""
    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs",
                        lambda class_types, *, lockfile_path=None:
                        [PINNED_FAKE] if set(class_types) & PINNED_FAKE.classes else [])
    lock = tmp_path / "custom_nodes.lock"
    lock.write_text(LOCK_TEXT)
    return lock


def test_attached_url_pack_commands_with_pinned_commit(fake_pack_with_lock):
    readiness = asset_gaps.attached_asset_readiness(
        "z-image", nodes=["AstridFakeNode"], lockfile_path=fake_pack_with_lock,
        custom_nodes="/srv/ComfyUI/custom_nodes")
    issue = readiness["issues"][0]
    folder = "/srv/ComfyUI/custom_nodes/ComfyUI-Fake"
    expected = [["git", "clone", "https://github.com/example/ComfyUI-Fake.git", folder],
                ["git", "-C", folder, "checkout", PINNED]]
    assert issue["details"]["commands"] == expected
    assert issue["details"]["after"] == asset_gaps.PACK_AFTER
    assert readiness["fixes"][0]["details"]["commands"] == expected
    assert readiness["fixes"][0]["scope"] == "attached_target"
    message = issue["message"]
    assert message.startswith("Your ComfyUI does not have AstridFakeNode, which comes from the custom node pack ComfyUI-Fake.")
    assert f"Commands: git clone https://github.com/example/ComfyUI-Fake.git /srv/ComfyUI/custom_nodes/ComfyUI-Fake ; " \
           f"git -C /srv/ComfyUI/custom_nodes/ComfyUI-Fake checkout {PINNED}. " in message
    assert message.endswith(asset_gaps.PACK_AFTER)
    _assert_no_align(readiness)


def test_attached_url_pack_commands_without_pinned_commit(known_packs, tmp_path):
    readiness = asset_gaps.attached_asset_readiness(
        "z-image", nodes=["AstridFakeNode"], lockfile_path=tmp_path / "missing.lock")
    details = readiness["issues"][0]["details"]
    placeholder = "<your ComfyUI folder>/custom_nodes/ComfyUI-Fake"
    assert details["commands"] == [["git", "clone", "https://github.com/example/ComfyUI-Fake.git", placeholder]]
    assert len(readiness["fixes"][0]["details"]["commands"]) == 1
    assert "checkout" not in readiness["issues"][0]["message"]
    assert "Commands: git clone https://github.com/example/ComfyUI-Fake.git '<your ComfyUI folder>/custom_nodes/ComfyUI-Fake'" \
        in readiness["issues"][0]["message"]
    _assert_no_align(readiness)


def test_attached_windows_custom_nodes_path_with_space_is_quoted(fake_pack_with_lock):
    readiness = asset_gaps.attached_asset_readiness(
        "z-image", nodes=["AstridFakeNode"], lockfile_path=fake_pack_with_lock,
        custom_nodes="C:\\My ComfyUI\\custom_nodes")
    folder = "C:\\My ComfyUI\\custom_nodes\\ComfyUI-Fake"
    assert readiness["issues"][0]["details"]["commands"][0] == [
        "git", "clone", "https://github.com/example/ComfyUI-Fake.git", folder]
    message = readiness["issues"][0]["message"]
    assert (f'Commands: git clone https://github.com/example/ComfyUI-Fake.git "{folder}" ; '
            f'git -C "{folder}" checkout {PINNED}. ') in message
    assert "\\" in message and "/My" not in message


def test_attached_two_nodes_from_one_pack_give_one_fix(fake_pack_with_lock):
    readiness = asset_gaps.attached_asset_readiness(
        "z-image", nodes=["AstridFakeNode", "AstridFakeNodeTwo"], lockfile_path=fake_pack_with_lock,
        custom_nodes="/srv/custom_nodes")
    assert len(readiness["issues"]) == 2
    assert all(issue["details"]["pack"] == "ComfyUI-Fake" for issue in readiness["issues"])
    assert len(readiness["fixes"]) == 1
    assert readiness["fixes"][0]["details"]["commands"][0][0:2] == ["git", "clone"]
    assert len(readiness["fixes"][0]["details"]["commands"]) == 2
    _assert_no_align(readiness)


def test_attached_core_node_has_no_commands(known_packs):
    readiness = asset_gaps.attached_asset_readiness("z-image", nodes=["CLIPLoader"])
    assert "commands" not in readiness["issues"][0]["details"]
    assert readiness["fixes"] == [{"scope": "attached_target", "details": {"action": "Update ComfyUI, then retry."}}]


def test_attached_pack_without_url_is_unchanged(monkeypatch, tmp_path):
    local = CustomNodePack(name="LocalPack", repo="local-copy", classes=frozenset({"AstridFakeNode"}))
    monkeypatch.setattr("vibecomfy.node_packs.resolve_node_packs",
                        lambda class_types, *, lockfile_path=None: [local])
    readiness = asset_gaps.attached_asset_readiness("z-image", nodes=["AstridFakeNode"],
                                                    lockfile_path=tmp_path / "x.lock")
    issue = readiness["issues"][0]
    assert issue["details"] == {"node": "AstridFakeNode", "pack": "LocalPack", "repository": None}
    assert issue["message"].endswith(
        "Install LocalPack in your ComfyUI's custom_nodes folder, then restart ComfyUI yourself and retry.")
    assert readiness["fixes"][0]["details"] == {
        "action": "Install LocalPack in your ComfyUI's custom_nodes folder, then restart ComfyUI yourself and retry."}
    _assert_no_align(readiness)


def test_attached_url_pack_message_keeps_user_in_charge(fake_pack_with_lock):
    readiness = asset_gaps.attached_asset_readiness(
        "z-image", nodes=["AstridFakeNode"], lockfile_path=fake_pack_with_lock)
    message = readiness["issues"][0]["message"]
    assert "restart ComfyUI yourself" in message
    assert "your ComfyUI" in message
    assert "Astrid will" not in message and "Astrid installs" not in message
