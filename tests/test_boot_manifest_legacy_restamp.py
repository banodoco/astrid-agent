from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core._shared import boot_manifest as bm
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.gateway.boot_conformance import VIBE_PROFILE_REGISTRY, vibe_profile_specs
from astrid.core.gateway.dispatch import compose_profile_handoff


FIXTURES = Path(__file__).parent / "fixtures" / "boot_manifest"
# Historical writers for all four releases produce byte-identical stamps.
# Each release's commit and capture command are in its sibling provenance note.
RELEASE_FIXTURES = {release: "r9-stamp.json" for release in ("r9", "r10", "r11", "r12")}


def _stored(tmp_path: Path, release: str = "r9") -> tuple[Path, Path]:
    support = tmp_path / "runtime"
    path = support / "astrid-host" / bm.BOOT_MANIFEST_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes((FIXTURES / RELEASE_FIXTURES[release]).read_bytes())
    return support, path


def _live() -> dict:
    return bm.build_manifest(registry=VIBE_PROFILE_REGISTRY, fixtures=vibe_profile_specs())


def _stamp(support: Path, path: Path) -> dict:
    return bm.stamp_boot_manifest(path, support_root=support,
                                  registry=VIBE_PROFILE_REGISTRY, fixtures=vibe_profile_specs())


@pytest.mark.parametrize("release", list(RELEASE_FIXTURES))
def test_real_legacy_stamp_is_atomically_replaced_once(tmp_path, monkeypatch, capsys, release):
    support, path = _stored(tmp_path, release)
    original = path.read_bytes()
    assert json.loads(original)["profile_order"] == ["pip_embedded", "checkout_server"]
    replace = bm.platform.replace_path
    replacements = []

    def checked_replace(source, destination):
        assert Path(destination) == path
        assert Path(source).parent == path.parent
        assert path.read_bytes() == original
        assert json.loads(Path(source).read_text()) == _live()
        replacements.append((source, destination))
        replace(source, destination)

    monkeypatch.setattr(bm.platform, "replace_path", checked_replace)
    assert _stamp(support, path) == _live()
    assert json.loads(path.read_text()) == _live()
    assert not list(path.parent.glob(".boot-manifest-*.tmp"))
    assert len(replacements) == 1
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "pip_embedded" in lines[0] and "checkout_server" in lines[0]
    identity = path.stat()
    assert _stamp(support, path) == _live()
    assert path.stat() == identity
    assert len(replacements) == 1
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("release", list(RELEASE_FIXTURES))
def test_legacy_hash_stays_read_only_until_composition(tmp_path, release):
    support, path = _stored(tmp_path, release)
    original = path.read_bytes()
    old_hash = bm.manifest_hash(json.loads(original))
    assert bm.load_boot_manifest_hash(path, support_root=support) == old_hash
    assert path.read_bytes() == original
    handoff = compose_profile_handoff(path, support_root=support)
    assert handoff["manifest"] == _live()
    assert handoff["sha256"] == bm.manifest_hash(_live()) != old_hash
    host = GenericPackHost(pack_roots=[tmp_path], boot_manifest_path=path,
                           boot_manifest_hash=handoff["sha256"])
    assert host.boot_manifest_provenance() == {
        "kind": "astrid.boot_manifest", "sha256": handoff["sha256"],
    }


@pytest.mark.parametrize("release", list(RELEASE_FIXTURES))
def test_bootstrap_restamps_before_launch_reads_hash(tmp_path, monkeypatch, release):
    from tests.sdk.test_host_recovery import Harness

    h = Harness(tmp_path, monkeypatch)
    path = h.support / "astrid-host" / bm.BOOT_MANIFEST_FILENAME
    path.parent.mkdir()
    path.write_bytes((FIXTURES / RELEASE_FIXTURES[release]).read_bytes())
    launch = h._popen

    def checked_launch(argv, **kwargs):
        assert json.loads(path.read_text()) == _live()
        digest = argv[argv.index("--boot-manifest-hash") + 1]
        assert digest == bm.load_boot_manifest_hash(path, support_root=h.support)
        return launch(argv, **kwargs)

    monkeypatch.setattr("astrid.sdk.host_bootstrap.popen_owned_group", checked_launch)
    try:
        receipt = h.ensure()
        assert receipt["host_boot_manifest_hash"] == bm.manifest_hash(_live())
        assert len(h.launches) == 1
    finally:
        h.cleanup()


@pytest.mark.parametrize("release", list(RELEASE_FIXTURES))
def test_resident_attach_preserves_legacy_completion_provenance(tmp_path, monkeypatch, release):
    from astrid.sdk import host_bootstrap
    from tests.sdk.test_host_recovery import Harness

    h = Harness(tmp_path, monkeypatch)
    try:
        h.ensure()
        path = h.support / "astrid-host" / bm.BOOT_MANIFEST_FILENAME
        original = (FIXTURES / RELEASE_FIXTURES[release]).read_bytes()
        path.write_bytes(original)
        old_hash = bm.load_boot_manifest_hash(path, support_root=h.support)
        for name in ("generic-host.json", "generic-host.ready.json"):
            marker = h.marker(name)
            marker["boot_manifest_hash"] = old_hash
            (h.support / name).write_text(json.dumps(marker))
        monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
        monkeypatch.setattr("astrid.core.gateway.dispatch.compose_profile_handoff",
                            lambda *_a, **_kw: pytest.fail("resident attach must preserve its stamp"))
        receipt = host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
        assert receipt["host_boot_manifest_hash"] == old_hash
        assert path.read_bytes() == original
        assert len(h.launches) == 1
    finally:
        h.cleanup()


def test_same_version_digest_drift_still_raises(tmp_path, capsys):
    support, path = _stored(tmp_path)
    current = _live()
    assert current["profile_order"] == []
    current["registry_digest"] = "0" * 64
    path.write_text(json.dumps(current))
    original = path.read_bytes()
    with pytest.raises(bm.BootManifestDrift):
        _stamp(support, path)
    assert path.read_bytes() == original
    assert capsys.readouterr().err == ""


def test_removed_and_unknown_profile_still_raises(tmp_path):
    support, path = _stored(tmp_path)
    stored = json.loads(path.read_text())
    stored["profile_order"].append("unknown_other")
    stored["profile_digests"]["unknown_other"] = "0" * 64
    path.write_text(json.dumps(stored))
    original = path.read_bytes()
    with pytest.raises(bm.BootManifestDrift):
        _stamp(support, path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutation", ["extra", "secret", "missing", "bad_order", "non_string"])
def test_malformed_legacy_stamp_is_not_migrated(tmp_path, mutation):
    support, path = _stored(tmp_path)
    stored = json.loads(path.read_text())
    if mutation == "extra":
        stored["unknown"] = "extra field"
    elif mutation == "secret":
        stored["profile_digests"]["api_key"] = "0" * 64
    elif mutation == "missing":
        del stored["profile_digests"]
    elif mutation == "bad_order":
        stored["profile_order"] = "pip_embedded"
    else:
        stored["profile_order"].append({"profile_id": "checkout_server"})
    path.write_text(json.dumps(stored))
    original = path.read_bytes()
    with pytest.raises(bm.BootManifestCorrupt):
        _stamp(support, path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("contents", ["not JSON", "[]"])
def test_corrupt_legacy_file_still_raises(tmp_path, contents):
    support, path = _stored(tmp_path)
    path.write_text(contents)
    with pytest.raises(bm.BootManifestCorrupt):
        _stamp(support, path)
    assert path.read_text() == contents


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_unsafe_legacy_path_still_raises(tmp_path, kind):
    support, path = _stored(tmp_path)
    original = path.read_bytes()
    path.unlink()
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_bytes(original)
        path.symlink_to(target)
    else:
        path.mkdir()
    with pytest.raises(bm.BootManifestCorrupt):
        _stamp(support, path)
    if kind == "symlink":
        assert path.is_symlink()
        assert target.read_bytes() == original
    else:
        assert path.is_dir()


def test_failed_legacy_replace_preserves_stamp_and_cleans_temp(tmp_path, monkeypatch, capsys):
    support, path = _stored(tmp_path)
    original = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(bm.platform, "replace_path", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        _stamp(support, path)
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".boot-manifest-*.tmp"))
    assert capsys.readouterr().err == ""
