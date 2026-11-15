"""A rejected resident attachment cannot change the running host's provenance."""

import json

import pytest

from astrid.core._shared import boot_manifest as bm
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.sdk import host_bootstrap
from tests.helpers.bootmanifest2_r12 import LEGACY_FIXTURE, r12_export, r12_probe
from tests.sdk.test_host_recovery import Harness


@pytest.mark.parametrize("existing", [True, False])
def test_composition_preview_leaves_all_persistent_state_untouched(tmp_path, existing, capsys):
    path = tmp_path / "astrid-host/boot-manifest.json"
    if existing:
        path.parent.mkdir()
        path.write_bytes(LEGACY_FIXTURE.read_bytes())
    before = {entry: entry.stat() for entry in tmp_path.rglob("*")}
    handoff = compose_profile_handoff(path, support_root=tmp_path, write=False)
    assert handoff["manifest"]["profile_order"] == []
    assert {entry: entry.stat() for entry in tmp_path.rglob("*")} == before
    if existing:
        assert path.read_bytes() == LEGACY_FIXTURE.read_bytes()
    assert capsys.readouterr().err == ""


@pytest.fixture
def legacy_resident(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.ensure()
    h.launches.clear()
    path = h.support / "astrid-host/boot-manifest.json"
    original = LEGACY_FIXTURE.read_bytes()
    path.write_bytes(original)
    old_hash = bm.load_boot_manifest_hash(path, support_root=h.support)
    for name in ("generic-host.json", "generic-host.ready.json"):
        marker = h.marker(name)
        marker.update(boot_manifest_hash=old_hash, source_inventory_identity="previous-inventory")
        (h.support / name).write_text(json.dumps(marker))
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    try:
        yield h, path, original, old_hash
    finally:
        h.cleanup()


def test_busy_rejected_attachment_preserves_stamp_and_provenance(legacy_resident, monkeypatch, capsys):
    h, path, original, old_hash = legacy_resident
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_a: False)
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda *_a, **_k: pytest.fail("busy host retired"))
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="busy with active child processes"):
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
    stderr = capsys.readouterr().err
    assert "astrid.pack_host_attach_rejected" in stderr
    assert "source_inventory_identity" in stderr
    assert "restamped" not in stderr
    assert path.read_bytes() == original
    assert not path.with_name(path.name + ".pre-restamp").exists()
    host = GenericPackHost(pack_roots=[h.source], boot_manifest_path=path, boot_manifest_hash=old_hash)
    assert host.boot_manifest_provenance()["sha256"] == old_hash
    assert not h.launches


@pytest.mark.parametrize("refused", [False, True])
def test_replacement_restamps_only_after_retirement_is_permitted(legacy_resident, monkeypatch, refused):
    h, path, original, _old_hash = legacy_resident
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_a: True)
    events = []

    def retire(*_a, **_kw):
        assert path.read_bytes() == original
        events.append("retire")
        if refused:
            raise host_bootstrap.PackHostBootstrapError("retirement blocked")

    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", retire)
    launch = h._popen

    def start(argv, **kwargs):
        assert events == ["retire"]
        assert json.loads(path.read_text())["profile_order"] == []
        assert path.with_name(path.name + ".pre-restamp").read_bytes() == original
        events.append("start")
        return launch(argv, **kwargs)

    monkeypatch.setattr(host_bootstrap, "popen_owned_group", start)
    if refused:
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="retirement blocked"):
            host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
        assert path.read_bytes() == original
        assert events == ["retire"]
    else:
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
        assert events == ["retire", "start"]


@pytest.mark.integration
@pytest.mark.linux_native
def test_busy_rejected_attachment_preserves_r12_resident_reader(legacy_resident, monkeypatch, r12_export):
    h, path, original, old_hash = legacy_resident
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_a: False)
    assert r12_probe(r12_export, h.support, resident_hash=old_hash)["outcome"] == "PASS"
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="busy"):
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
    result = r12_probe(r12_export, h.support, resident_hash=old_hash)
    assert result["outcome"] == "PASS", result
    assert path.read_bytes() == original
