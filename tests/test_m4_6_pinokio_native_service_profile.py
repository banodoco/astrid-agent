"""Source predicate tests; synthetic files confer no native authority."""

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from scripts import m4_6_pinokio_custody_provider as provider
from scripts.journey_matrix import M4_6_MATRIX


ROWS = [row for row in M4_6_MATRIX if row["job_id"].startswith("pinokio-")]


@pytest.fixture
def source_fixture(tmp_path, monkeypatch):
    # Test the verifier mechanics against a complete synthetic inventory. The
    # exact selected Git bytes are separately checked by the task's source probe.
    manifest = {}
    for relative in provider._U2_SOURCE_SHA256:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"fixture: {relative}\n".encode()
        path.write_bytes(data)
        manifest[relative] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(provider, "_U2_SOURCE_SHA256", manifest)
    return tmp_path


def bind(root, row=ROWS[0], **kwargs):
    return provider.PinokioNativeServiceProfile.bind_source(
        slot_id=row["job_id"], contract=row, app_root=root,
        source_commit=provider.U2_PINOKIO_COMMIT, **kwargs,
    )


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["job_id"])
def test_six_profiles_bind_distinct_modes_actions_and_keep_native_identities_unbound(source_fixture, row):
    profile = bind(source_fixture, row)
    local = row["mode"] == "pinokio-fully-local"
    assert (profile.slot_id, profile.mode, profile.os_name) == (row["job_id"], row["mode"], row["os"])
    assert profile.pinokio_mode == ("local" if local else "backend")
    assert profile.action_roles == {
        "install": "pinokio/install-local.js" if local else "pinokio/install.js",
        "start": "pinokio/start.js", "stop": "pinokio/stop.js",
        "update": "pinokio/update.js", "uninstall": "pinokio/uninstall.js",
    }
    assert profile.source_owner_mapping == {"managed_by": "pinokio", "agent_os_registration": "bypassed"}
    if row["os"] == "windows":
        assert profile.runtime_command_shape == (
            "<environmentCmd(installed.env)>", "/d", "/s", "/c",
            '""<program-root>\\run.cmd"' + (' --hosted-origin off' if local else '') + '"',
        )
    else:
        assert profile.runtime_command_shape == (
            "<program-root>/run", *(("--hosted-origin", "off") if local else ()),
        )
    assert all(value is None for value in profile.unbound_identities.values())
    assert profile.unbound_identities["native_manager_target_inventory"] is None
    assert profile.unbound_identities["canonical_native_definitions"] is None
    assert ("editor_script_and_dependency_closure" in profile.unbound_identities) == local
    profile.unbound_identities.clear()  # A returned dictionary cannot supply authority.
    profile.verify_current_bytes()
    with pytest.raises(provider.PinokioCustodyRefused, match="unbound native/external identities"):
        profile.require_native_binding()


@pytest.mark.parametrize("relative", [
    "pinokio/install.js", "pinokio/install-local.js", "pinokio/shell.mjs",
    "pinokio/installer-lock.py", "pinokio/update-stage.mjs",
])
def test_profile_rechecks_install_dynamic_and_path_executed_helper_bytes(source_fixture, relative):
    profile = bind(source_fixture)
    path = source_fixture / relative
    original = path.read_bytes()
    path.write_bytes(b"X" + original[1:])  # Equal-size drift still refuses.
    for check in (profile.verify_current_bytes, profile.require_native_binding):
        with pytest.raises(provider.PinokioSourceMismatch, match="selected source bytes differ"):
            check()


def test_profile_rejects_unknown_commit_and_production_manifest_rejects_fabricated_source(tmp_path):
    row = ROWS[0]
    with pytest.raises(provider.PinokioSourceMismatch, match="exact selected Pinokio source commit"):
        provider.PinokioNativeServiceProfile.bind_source(
            slot_id=row["job_id"], contract=row, app_root=tmp_path, source_commit="0" * 40,
        )
    first = tmp_path / sorted(provider._U2_SOURCE_SHA256)[0]
    first.parent.mkdir(parents=True)
    first.write_text("fabricated source")
    with pytest.raises(provider.PinokioSourceMismatch, match="selected source bytes differ"):
        bind(tmp_path)


@pytest.mark.parametrize("kind", ["missing", "leaf-symlink", "parent-symlink", "root-symlink"])
def test_profile_rejects_missing_or_redirected_source(source_fixture, kind):
    root = source_fixture
    if kind == "root-symlink":
        root = source_fixture / "redirect"
        root.symlink_to(source_fixture, target_is_directory=True)
    elif kind == "parent-symlink":
        parent = source_fixture / "pinokio/platforms"
        moved = source_fixture / "moved-platforms"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    else:
        path = source_fixture / "pinokio/shell.mjs"
        data = path.read_bytes()
        path.unlink()
        if kind == "leaf-symlink":
            target = source_fixture / "shell-copy"
            target.write_bytes(data)
            path.symlink_to(target)
    with pytest.raises(provider.PinokioSourceMismatch):
        bind(root)


def test_profile_rejects_crossed_rows_unknown_identity_fields_and_forged_mode(source_fixture):
    for source in ROWS:
        for other in ROWS:
            if source == other:
                continue
            with pytest.raises(provider.PinokioCustodyRefused, match="fixed row"):
                provider.PinokioNativeServiceProfile.bind_source(
                    slot_id=source["job_id"], contract=other, app_root=source_fixture,
                    source_commit=provider.U2_PINOKIO_COMMIT,
                )
    for field in ("principal", "engine_identity", "native_manager_target_inventory"):
        row = dict(ROWS[0], **{field: "plausible-but-untrusted"})
        with pytest.raises(provider.PinokioCustodyRefused, match="fixed row"):
            bind(source_fixture, row)
    profile = bind(source_fixture)
    forged = replace(profile, mode="pinokio-fully-local" if profile.mode == "pinokio-backend" else "pinokio-backend")
    with pytest.raises(provider.PinokioSourceMismatch, match="binding changed"):
        forged.require_native_binding()


def test_source_profile_cannot_dispatch_or_add_install_to_generic_action_graph(source_fixture):
    profile = bind(source_fixture)
    assert not hasattr(profile, "action_for")
    graph = provider.PinokioActionGraph.collect(source_fixture)
    with pytest.raises(ValueError, match="only start, stop, update, and uninstall"):
        graph.action_for("install")
    assert len(provider.U2_DYNAMIC_IMPORT_SITES) == 12
    assert [site for site in provider.U2_DYNAMIC_IMPORT_SITES if site[2].startswith("node:")] == [
        ("pinokio/update-stage.mjs", 137, "node:fs/promises"),
    ]
    assert "node:fs/promises" in provider.U2_NODE_BUILTINS
