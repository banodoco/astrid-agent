"""Only the complete authenticated historical boot stamp is eligible for restamping."""

import json

import pytest

from astrid.core._shared import boot_manifest as bm
from astrid.core.gateway.dispatch import compose_profile_handoff
from tests.helpers.bootmanifest2_r12 import LEGACY_FIXTURE


def _path(tmp_path, manifest):
    path = tmp_path / "astrid-host/boot-manifest.json"
    path.parent.mkdir()
    path.write_text(json.dumps(manifest))
    return path


@pytest.mark.parametrize("mutation", ["only_removed", "one_digest", "registry", "conformance", "fixture", "reverse_order", "missing_profile_digest"])
def test_nonhistorical_legacy_content_raises_drift(tmp_path, mutation):
    stored = json.loads(LEGACY_FIXTURE.read_text())
    if mutation == "only_removed":
        stored.update(profile_order=["pip_embedded"], profile_digests={"pip_embedded": "0" * 64},
                      registry_digest="0" * 64, conformance_digest="0" * 64, fixture_digests={})
    elif mutation == "one_digest":
        stored["profile_digests"]["pip_embedded"] = "0" * 64
    elif mutation == "registry":
        stored["registry_digest"] = "0" * 64
    elif mutation == "conformance":
        stored["conformance_digest"] = "0" * 64
    elif mutation == "fixture":
        stored["fixture_digests"]["attempt-output-fixture.json"] = "0" * 64
    elif mutation == "reverse_order":
        stored["profile_order"].reverse()
    else:
        del stored["profile_digests"]["pip_embedded"]
    path = _path(tmp_path, stored)
    original = path.read_bytes()
    with pytest.raises(bm.BootManifestDrift):
        compose_profile_handoff(path, support_root=tmp_path)
    assert path.read_bytes() == original
    assert not path.with_name(path.name + ".pre-restamp").exists()


def test_internally_consistent_custom_legacy_registry_raises_drift(tmp_path):
    order = ["pip_embedded", "checkout_server"]
    registry = {name: {"definition_version": 2, "binding": "custom.probe",
                       "output_policy": "none", "probe": "accepted"} for name in order}
    fixtures = [{"profile_id": name, "accepted_input": "custom", "fixture_digests": {}}
                for name in order]
    custom = bm.build_manifest(registry=registry, fixtures=fixtures, profile_order=order)
    assert custom != json.loads(LEGACY_FIXTURE.read_text())
    path = _path(tmp_path, custom)
    original = path.read_bytes()
    with pytest.raises(bm.BootManifestDrift):
        compose_profile_handoff(path, support_root=tmp_path)
    assert path.read_bytes() == original


def test_known_legacy_stamp_does_not_authorize_custom_target_registry(tmp_path):
    path = _path(tmp_path, json.loads(LEGACY_FIXTURE.read_text()))
    registry = {"custom": {"definition_version": 1, "binding": "custom.probe",
                           "output_policy": "none", "probe": "accepted"}}
    fixtures = [{"profile_id": "custom", "fixture_digests": {}}]
    original = path.read_bytes()
    with pytest.raises(bm.BootManifestDrift):
        bm.stamp_boot_manifest(path, support_root=tmp_path, registry=registry,
                               fixtures=fixtures, profile_order=["custom"])
    assert path.read_bytes() == original
