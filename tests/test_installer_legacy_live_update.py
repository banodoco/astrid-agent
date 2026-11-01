"""The frozen release-0 exception is confined to the live update gates."""
import copy
import json
from pathlib import Path

import pytest

from astrid.installer import legacy_identity, update_job
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, write_json
from astrid.installer.service import render
from tests.test_installer_lifecycle import LifecycleBackend


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    source = json.loads((Path(__file__).parent / "fixtures/upgrade/legacy_identityless_v31.json").read_text())
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "3204bb00-6d53a190-example"
    for path in (folder / "r", folder / "py/bin", folder / "a/source"):
        path.mkdir(parents=True)
    python, profile = folder / "py/bin/python", folder / "profile.json"
    python.write_text("fixture interpreter")
    declaration = folder / "a/service.json"
    declaration.write_text("{}")
    write_json(profile, {"runtime_checkout": str(folder / "r"), "runtime_environment": str(folder / "py"),
                         "source_checkout": str(folder / "a/source"), "service_declaration": str(declaration)})
    manifest = copy.deepcopy(source["manifest"])
    manifest["vibecomfy"] = {"sha": "ac0ee1b80991f2414dae27d264af4b5e2890a7a6"}
    write_json(folder / "manifest.json", manifest)
    write_json(folder / "version.json", {"root": str(folder), "python": str(python), "profile": str(profile), "env": {}})
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "data_root": str(data)})
    (root / "current").write_text(folder.name)
    discovery = {**source["discovery"], "endpoint": "http://127.0.0.1:1234", "pid": 123, "process_birth_id": "birth"}
    health = copy.deepcopy(source["health"])
    path = data / "runtime/discovery.json"
    write_json(path, discovery)
    monkeypatch.setattr("runtime_protocol.platform.process_identity", lambda pid: {"pid": pid, "birth_id": "birth"})
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda self, **kwargs: health)
    contract = {"python": str(python), "python_version": "3.11.13", "protocol": legacy_identity.PROTOCOL,
                "schema_digest": legacy_identity.SCHEMA_DIGEST, "workspace_schema_version": 31,
                **{name: str(folder / "py" / (name + ".py")) for name in
                   ("astrid", "astrid_runtime", "runtime_protocol", "astrid_runtime_client")}}
    monkeypatch.setattr(legacy_identity, "_contract", lambda version: contract)
    return root, data, health, discovery, contract


def gate(root, data):
    backend = Backend(root, root / "install.log", root / "uv", process_control="strict")
    assert backend.source_checkout_ownership(data, current(root)) is False
    return backend


@pytest.mark.parametrize("system,service_name", [("linux", "[Service]"), ("darwin", "<plist"), ("win32", "<Task")])
def test_live_legacy_update_reaches_stop_migrate_restart(legacy, system, service_name):
    root, data, _, _, _ = legacy
    old = current(root)
    assert service_name in render(system, old, data, root / "service.log")
    assert gate(root, data)._legacy_identity_admitted
    backend = LifecycleBackend(root, data)
    backend.old = old
    Installer(root, data, backend, say=lambda line: None).run("update", {"release": 1})
    calls = backend.calls
    old_stop = next(i for i, call in enumerate(calls) if call[0] == "stop" and call[1] == old)
    migration = next(i for i, call in enumerate(calls) if call[0] == "upgrade")
    restart = next(i for i, call in enumerate(calls) if call[0] == "start")
    assert old_stop < migration < restart
    assert calls[migration][1] == backend.candidate
    assert current(root) == backend.candidate


@pytest.mark.parametrize("change", ["health_digest", "health_protocol", "health_status", "health_instance",
                                   "discovery_identity", "health_identity", "version_identity", "manifest",
                                   "selection", "manager", "contract_digest", "contract_protocol", "contract_schema",
                                   "contract_origin", "profile", "record_fields"])
def test_nonmatching_or_partial_identity_is_refused(legacy, change):
    root, data, health, discovery, contract = legacy
    old = current(root)
    if change.startswith("health_"):
        key = {"health_digest": "schema_digest", "health_protocol": "protocol", "health_status": "status",
               "health_instance": "runtime_instance_id", "health_identity": "identity"}[change]
        health[key] = {"release": 0, "managed_by": "source"} if key == "identity" else "different"
    elif change == "discovery_identity":
        discovery["identity"] = {"release": 0, "managed_by": None}
        write_json(data / "runtime/discovery.json", discovery)
    elif change in ("version_identity", "record_fields"):
        record = json.loads((old.root / "version.json").read_text())
        record["identity" if change == "version_identity" else "extra"] = None
        write_json(old.root / "version.json", record)
    elif change == "manifest":
        with (old.root / "manifest.json").open("a") as stream:
            stream.write(" ")
    elif change == "selection":
        (root / "current").write_text("different")
    elif change == "manager":
        write_json(root / "install.json", {"owner": "haus.astrid.runtime", "managed_by": "pinokio"})
    elif change.startswith("contract_"):
        contract[{"contract_digest": "schema_digest", "contract_protocol": "protocol", "contract_schema": "workspace_schema_version", "contract_origin": "astrid_runtime"}[change]] = "different"
    elif change == "profile":
        write_json(old.profile, {"runtime_checkout": "/foreign"})
    assert not legacy_identity.admitted(old, health, discovery, program_root=root)
    if change not in ("health_identity", "selection", "manager", "version_identity"):
        with pytest.raises((RuntimeError, ValueError, OSError)):
            gate(root, data)
        with pytest.raises((ValueError, OSError)):
            update_job._probe_selected_identity(data, old, root)


def test_native_rechecks_legacy_tuple_and_discovery(legacy, monkeypatch):
    root, data, health, _, _ = legacy
    old = current(root)
    instance, identity = update_job._probe_selected_identity(data, old, root)
    assert (instance, identity) == (health["runtime_instance_id"], {"release": 0, "managed_by": None})
    health["schema_digest"] = "sha256:wrong"
    with pytest.raises(ValueError):
        update_job._probe_selected_identity(data, old, root)


def test_legacy_exception_requires_update_context(legacy):
    root, data, _, _, _ = legacy
    backend = Backend(root, root / "install.log", root / "uv")
    with pytest.raises(RuntimeError):
        backend.source_checkout_ownership(data, current(root))


@pytest.mark.parametrize("request_identity", [None, {"release": 0, "managed_by": None}])
def test_native_acceptance_and_worker_binding_revalidate(legacy, tmp_path, monkeypatch, request_identity):
    from tests.test_installer_update_job import _fixture
    root, data, health, _, _ = legacy
    real_probe = update_job._probe_identity
    _, _, _, request = _fixture(tmp_path / "job-inputs", monkeypatch)
    monkeypatch.setattr(update_job, "_probe_identity", real_probe)
    selection = Path(request["selection_path"])
    write_json(selection, {"owner": "haus.astrid.runtime",
                           "program_root": str(root), "data_root": str(data)})
    old = current(root)
    request.update(root=str(root), data_root=str(data), old_version=old.document(),
                   old_identity=request_identity,
                   old_instance_id=health["runtime_instance_id"],
                   log_path=str(root / "logs/update-test.log"))
    job = update_job.accept(request)
    assert job["old_identity"] == {"release": 0, "managed_by": None}
    job["worker_command_sha256"] = update_job._worker_binding_digest(old, job)
    assert update_job._validate_worker_binding(root, job) == (old, data)
    health["runtime_instance_id"] = "replacement"
    with pytest.raises(ValueError):
        update_job._validate_worker_binding(root, job)


def test_frozen_policy_matches_reviewed_release_manifest():
    releases = json.loads((Path(__file__).parent / "upgrade/releases.json").read_text())
    policy = next(row for row in releases["releases"] if row["id"] == "served")["supported_legacy_identity_schema"]
    assert policy["installed_manifest_sha256"] == legacy_identity.MANIFEST_SHA256
    assert policy["agent_sha"] == legacy_identity.AGENT_SHA
    assert policy["runtime_sha"] == legacy_identity.RUNTIME_SHA
    assert policy["schema_digest"] == legacy_identity.SCHEMA_DIGEST
    assert policy["protocol"] == legacy_identity.PROTOCOL
    assert policy["workspace_schema_version"] == 31
    assert policy["version_record_keys"] == ["env", "profile", "python", "root"]
    assert policy["version_id_prefix"] == "3204bb00-6d53a190-"
    assert policy["python_version"] == "3.11.13"


def test_qualification_and_live_admission_use_same_classifier(legacy, monkeypatch):
    from scripts.verify_installed import legacy_identity_observation

    root, data, health, discovery, contract = legacy
    old = current(root)
    calls = []
    original = legacy_identity.classify

    def spy(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result[0])
        return result

    monkeypatch.setattr(legacy_identity, "classify", spy)
    # The verifier imports the same classifier; spy on its bound name too.
    monkeypatch.setattr("scripts.verify_installed.classify", spy)
    raw = (old.root / "manifest.json").read_bytes()
    record = json.loads((old.root / "version.json").read_text())
    assert legacy_identity.admitted(old, health, discovery, program_root=root)
    assert legacy_identity_observation(health, discovery, record, json.loads(raw), raw,
                                       old.root / "version.json", contract)[0]
    assert calls == [True, True]
    assert gate(root, data)._legacy_identity_admitted
    update_job._probe_selected_identity(data, old, root)
    assert calls == [True, True, True, True]


def test_missing_shipped_table_fails_closed(legacy, monkeypatch):
    root, _, health, discovery, _ = legacy
    monkeypatch.setattr(legacy_identity, "_policy", lambda: None)
    assert not legacy_identity.admitted(current(root), health, discovery, program_root=root)


@pytest.mark.parametrize("discovery_state", ["missing", "stale"])
def test_selected_legacy_with_no_live_discovery_is_not_admitted(legacy, monkeypatch, discovery_state):
    root, data, _, _, _ = legacy
    path = data / "runtime/discovery.json"
    if discovery_state == "missing":
        path.unlink()
    else:
        monkeypatch.setattr("runtime_protocol.platform.process_identity", lambda pid: None)
    # Even an exact record, or an unavailable policy, cannot turn a missing
    # owner observation into a positive stopped proof.
    backend = Backend(root, root / "install.log", root / "uv", process_control="strict")
    with pytest.raises(RuntimeError, match="Cannot prove.*stopped"):
        backend.source_checkout_ownership(data, current(root))
    monkeypatch.setattr(legacy_identity, "_policy", lambda: None)
    with pytest.raises(RuntimeError, match="Cannot prove.*stopped"):
        backend.source_checkout_ownership(data, current(root))


def test_selected_legacy_live_identity_cannot_bypass_shipped_policy(legacy, monkeypatch):
    root, data, health, _, _ = legacy
    health["identity"] = {"release": 0, "managed_by": None}
    monkeypatch.setattr("astrid_runtime.identity.validate_live_owner_identity",
                        lambda *args, **kwargs: health["identity"])
    with pytest.raises(RuntimeError, match="Live Runtime startup identity cannot be verified"):
        gate(root, data)
