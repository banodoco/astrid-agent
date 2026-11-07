"""Installed-resource coverage for default agent skills."""

from importlib import resources

from astrid.skills import (
    _sync_descriptors_for_harness,
    default_pack_ids,
    state,
)
from astrid.core.pack import load_pack_manifest
from astrid.skills.discovery import PACKS_DIR, list_skills


def test_hivemind_is_not_bundled_into_the_astrid_package() -> None:
    root = resources.files("astrid")
    skill = root.joinpath("packs", "hivemind", "skill", "SKILL.md")
    manifest = root.joinpath("packs", "hivemind", "pack.yaml")
    assert not skill.exists()
    assert not manifest.exists()


def test_hivemind_is_not_hardcoded_as_a_default_skill() -> None:
    source_only = list_skills(PACKS_DIR)
    assert "_core" in default_pack_ids(source_only)
    # The package resource remains available for compatibility, but the
    # default skill set is source-provisioned rather than inferred from the
    # bundled Hivemind copy.
    assert "hivemind" not in default_pack_ids(source_only)


def test_default_skill_policy_comes_from_pack_manifests() -> None:
    descriptors = list_skills()
    default_ids = set(default_pack_ids(descriptors))
    declared_ids = {
        descriptor.pack_id
        for descriptor in descriptors
        if (descriptor.skill_dir.parent / "pack.yaml").is_file()
        if load_pack_manifest(descriptor.skill_dir.parent / "pack.yaml")
        .extensions.get("astrid", {}).get("skills", {}).get("default") is True
    }
    assert declared_ids == {"vibecomfy", "rendering", "typed_timeline", "video_editing"}
    assert declared_ids <= default_ids


def test_default_composition_preserves_opt_outs_without_acquisition(monkeypatch, tmp_path) -> None:
    descriptors = list_skills()
    current = state.load(tmp_path / "skills.json")
    current["disabled_defaults"]["codex"] = ["vibecomfy", "typed_timeline"]

    def forbidden_acquisition(*_args, **_kwargs):
        raise AssertionError("default skill composition must not provision sources or providers")

    monkeypatch.setattr("astrid.core.pack.source_setup.provision", forbidden_acquisition)
    selected = _sync_descriptors_for_harness(
        descriptors, current, "codex", deep=False,
    )
    selected_ids = {descriptor.pack_id for descriptor in selected}

    assert "_core" in selected_ids
    assert {"rendering", "video_editing"} <= selected_ids
    assert not {"vibecomfy", "typed_timeline"} & selected_ids


def test_retained_integrations_flow_through_existing_sync_selection(tmp_path) -> None:
    descriptors = list_skills()
    current = state.load(tmp_path / "skills.json")
    current["setup_selection"] = {"integrations": ["media"]}

    selected = _sync_descriptors_for_harness(
        descriptors,
        current,
        "codex",
        deep=False,
        selected_pack_ids=("understanding",),
    )
    selected_ids = {descriptor.pack_id for descriptor in selected}

    assert {"media", "understanding"} <= selected_ids


def test_default_uninstall_is_a_durable_opt_out_until_reinstall() -> None:
    data = state.load()
    state.record_install(data, "claude", "hivemind", target="/tmp/hivemind", mechanism="symlink")
    state.record_uninstall(data, "claude", "hivemind", default=True)
    assert data["disabled_defaults"]["claude"] == ["hivemind"]
    state.record_install(data, "claude", "hivemind", target="/tmp/hivemind", mechanism="symlink")
    assert data["disabled_defaults"]["claude"] == []
