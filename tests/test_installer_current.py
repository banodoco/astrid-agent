"""Public command recovery after a partial program removal."""

import os
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from astrid.installer import cli
from astrid.installer.core import Version, current, read_json, write_json
from astrid.installer.service import LABEL, default_program_root


@pytest.mark.parametrize("explicit_root", [False, True])
def test_partial_uninstall_can_finish_after_version_metadata_was_removed(
    tmp_path, monkeypatch, explicit_root,
):
    from runtime_protocol import platform

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    home = tmp_path / "home"
    root = tmp_path / "program"
    default_root = default_program_root(home, sys.platform)
    data = tmp_path / "workspace"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    (root / "current").write_text("removed-version\n")
    backend = Mock()
    backend.source_checkout_ownership.return_value = False
    backend.service.recorded.return_value = {
        "owner": LABEL, "program_root": str(root),
        "data_root": str(data), "uninstalled": True,
    }
    backend.existing.return_value = None
    backend.remove.return_value = []
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))

    arguments = (["--root", str(root)] if explicit_root else []) + ["--uninstall"]
    assert cli.main(arguments) == 0
    assert cli.Backend.call_args_list[-1].args[0] == root
    assert not (default_root / "install.json").exists()
    backend.stop.assert_called_once_with(None, data)
    backend.unregister.assert_called_once_with()
    backend.remove.assert_called_once_with(root, data, delete_data=False)


@pytest.mark.parametrize("entry", ["versions", "version"])
def test_absent_version_record_does_not_allow_linked_parents(tmp_path, entry):
    root, external = tmp_path / "program", tmp_path / "external"
    root.mkdir()
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("keep")
    (root / "current").write_text("removed-version\n")
    if entry == "versions":
        link = root / "versions"
    else:
        (root / "versions").mkdir()
        link = root / "versions/removed-version"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                       check=True, capture_output=True, text=True)
    else:
        link.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot contain links"):
        current(root)

    assert list(external.iterdir()) == [sentinel]
    assert sentinel.read_text() == "keep"


def test_present_malformed_version_record_is_not_treated_as_removed(tmp_path):
    write_json(tmp_path / "install.json", {"owner": LABEL})
    (tmp_path / "current").write_text("v1\n")
    record = tmp_path / "versions/v1/version.json"
    record.parent.mkdir(parents=True)
    record.write_text("[]")

    with pytest.raises(ValueError, match="Expected an object"):
        current(tmp_path)


def test_current_version_identifier_cannot_escape_program_root(tmp_path):
    (tmp_path / "current").write_text("../external\n")

    with pytest.raises(ValueError, match="Invalid installation version identifier"):
        current(tmp_path)


def test_current_record_cannot_point_outside_managed_versions(tmp_path):
    root, external = tmp_path / "program", tmp_path / "external"
    record = root / "versions/v1/version.json"
    version = Version(external, external / "bin/python", external / "profile.json", {})
    write_json(record, version.document())
    (root / "current").write_text("v1\n")

    with pytest.raises(ValueError, match="points outside the managed directory"):
        current(root)


@pytest.mark.parametrize("managed_by", [None, "pinokio"])
def test_version_identity_roundtrips_and_legacy_version_uses_manifest(tmp_path, managed_by):
    from astrid.installer.core import Version

    root = tmp_path / "program/versions/v1"
    root.mkdir(parents=True)
    version = Version(root, tmp_path / "python", tmp_path / "profile", {}, 37, managed_by)
    write_json(root / "version.json", version.document())
    loaded = Version.load(json.loads((root / "version.json").read_text()))
    assert loaded.release == 37
    assert loaded.managed_by == managed_by
    assert loaded.env["ASTRID_INSTALL_RELEASE"] == "37"
    assert loaded.env["ASTRID_INSTALL_MANAGED_BY"] == (managed_by or "")

    write_json(root / "manifest.json", {"release": 8})
    legacy = {key: value for key, value in version.document().items() if key != "identity"}
    write_json(root / "version.json", legacy)
    write_json(tmp_path / "program/install.json", {"owner": LABEL, "managed_by": managed_by})
    recovered = Version.load(legacy)
    assert recovered.release == 8
    assert recovered.managed_by == managed_by
    assert recovered.env["ASTRID_INSTALL_RELEASE"] == "8"
    assert recovered.env["ASTRID_INSTALL_MANAGED_BY"] == (managed_by or "")
    assert read_json(root / "version.json") == legacy  # Selection remains read-only.


@pytest.mark.parametrize("manifest,marker,error", [
    ({"release": True}, {}, "nonnegative integer"),
    ({"release": -1}, {}, "nonnegative integer"),
    ({"release": "8"}, {}, "nonnegative integer"),
    ({"release": 8}, {"managed_by": "source"}, "Invalid installation manager"),
])
def test_legacy_current_rejects_invalid_canonical_identity_without_writes(tmp_path, manifest, marker, error):
    root = tmp_path / "program"
    folder = root / "versions/v1"
    value = {"root": str(folder), "python": str(tmp_path / "python"),
             "profile": str(folder / "profile.json"), "env": {"ASTRID_INSTALL_RELEASE": "99"}}
    write_json(folder / "version.json", value)
    write_json(folder / "manifest.json", manifest)
    write_json(root / "install.json", marker)
    (root / "current").write_text("v1\n")
    before = (folder / "version.json").read_bytes()

    with pytest.raises(ValueError, match=error):
        current(root)

    assert (folder / "version.json").read_bytes() == before


def test_release_must_be_nonnegative_integer():
    from astrid.installer.core import install_release

    assert install_release({}) == 0
    assert install_release({"release": 12}) == 12
    for value in (-1, True, "12", 1.5):
        with pytest.raises(ValueError, match="nonnegative integer"):
            install_release({"release": value})


@pytest.mark.parametrize(
    ("managed_by", "expected"),
    [
        (None, "This Astrid is newer than astrid.haus offers. Try again when astrid.haus has a newer release."),
        ("pinokio", "This Astrid is newer than astrid.haus offers. Choose Pinokio's Update button when a newer release is available."),
    ],
)
def test_older_served_release_refusal_uses_install_type_action(tmp_path, managed_by, expected):
    from astrid.installer.core import Version, update_required

    old = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {}, 9, managed_by)
    with pytest.raises(ValueError) as caught:
        update_required("install", {"release": 8}, old, managed_by=managed_by)

    assert str(caught.value) == expected


def test_same_and_newer_served_releases_keep_repair_and_update_dispatch(tmp_path):
    from astrid.installer.core import Version, update_required

    old = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {}, 9)
    assert not update_required("install", {"release": 9}, old)
    assert update_required("install", {"release": 10}, old)


def test_explicit_null_version_identity_is_malformed(tmp_path):
    root = tmp_path / "versions/v1"
    root.mkdir(parents=True)
    value = {"root": str(root), "python": str(tmp_path / "python"),
             "profile": str(tmp_path / "profile"), "env": {}, "identity": None}
    with pytest.raises(ValueError, match="Invalid installation identity"):
        Version.load(value)


def test_managed_version_with_source_profile_is_still_an_installed_release(tmp_path):
    root = tmp_path / "program"
    version_root = root / "versions/v1"
    version_root.mkdir(parents=True)
    version = Version(version_root, tmp_path / "python", version_root / "profile.json", {}, 8, None)
    write_json(version_root / "version.json", version.document())
    write_json(version_root / "manifest.json", {"release": 8, "agent": {"sha": "a" * 40},
                                                   "runtime": {"sha": "b" * 40}})
    write_json(version_root / "profile.json", {
        "profile": "astrid", "runtime_checkout": str(version_root / "r"),
        "source_checkout": str(version_root / "a"),
    })
    (root / "current").write_text("v1\n", encoding="utf-8")

    selected = current(root)
    assert selected is not None
    assert selected.release == 8
    assert selected.managed_by is None
