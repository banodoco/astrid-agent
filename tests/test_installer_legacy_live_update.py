"""The frozen release-0 exception is confined to the live update gates."""
import copy
import json
import shutil
from pathlib import Path

import pytest

from astrid.installer import legacy_identity, update_job
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, write_json
from astrid.installer.service import render
from tests.test_installer_lifecycle import LifecycleBackend

SCOPED_R0_BIRTH = (
    "proc-start-ticks:19;boot:63de1c31-9fcc-4784-bf4c-7d3d3ce35985"
    ";pidns:7"
)


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
            update_job._probe_selected_identity(data, old, root, target_release=1)


@pytest.mark.parametrize("null_in", ["health", "discovery", "both"])
def test_explicit_null_identity_is_refused_without_mutating_discovery(legacy, null_in):
    root, data, health, discovery, _ = legacy
    old = current(root)
    path = data / "runtime/discovery.json"
    assert "identity" not in health and "identity" not in discovery
    if null_in in ("health", "both"):
        health["identity"] = None
    if null_in in ("discovery", "both"):
        discovery["identity"] = None
        write_json(path, discovery)
    snapshot = path.read_bytes()
    health_snapshot = copy.deepcopy(health)
    assert not legacy_identity.admitted(old, health, discovery, program_root=root)
    with pytest.raises(RuntimeError):
        gate(root, data)
    with pytest.raises(ValueError):
        update_job._probe_selected_identity(data, old, root, target_release=1)
    assert path.read_bytes() == snapshot
    assert health == health_snapshot


def test_legacy_refuses_valid_version_with_foreign_program_root(legacy, tmp_path):
    root, data, health, discovery, _ = legacy
    selected = current(root)
    foreign_root = tmp_path / "foreign-program"
    foreign_root.mkdir()

    assert legacy_identity.admitted(selected, health, discovery, program_root=root)
    assert not legacy_identity.admitted(selected, health, discovery, program_root=foreign_root)
    with pytest.raises(ValueError):
        update_job._probe_selected_identity(data, selected, foreign_root, target_release=1)


def test_legacy_refuses_selected_version_under_foreign_program_root(legacy, tmp_path, monkeypatch):
    root, data, health, discovery, contract = legacy
    selected = current(root)
    foreign_root = tmp_path / "foreign-program"
    shutil.copytree(root, foreign_root)
    foreign_folder = foreign_root / "versions" / selected.root.name

    record_path = foreign_folder / "version.json"
    record = json.loads(record_path.read_text())
    for key in ("root", "python", "profile"):
        record[key] = str(foreign_folder / Path(record[key]).relative_to(selected.root))
    write_json(record_path, record)
    profile_path = foreign_folder / "profile.json"
    profile = json.loads(profile_path.read_text())
    for key in ("runtime_checkout", "runtime_environment", "source_checkout", "service_declaration"):
        profile[key] = str(foreign_folder / Path(profile[key]).relative_to(selected.root))
    write_json(profile_path, profile)
    foreign = current(foreign_root)
    foreign_contract = {
        key: (str(foreign.root / Path(value).relative_to(selected.root))
              if key in {"python", "astrid", "astrid_runtime", "runtime_protocol", "astrid_runtime_client"}
              else value)
        for key, value in contract.items()
    }
    monkeypatch.setattr(legacy_identity, "_contract",
                        lambda version: foreign_contract if version.root == foreign.root else contract)

    assert legacy_identity.admitted(foreign, health, discovery, program_root=foreign_root)
    assert not legacy_identity.admitted(foreign, health, discovery, program_root=root)
    with pytest.raises(ValueError):
        update_job._probe_selected_identity(data, foreign, root, target_release=1)



@pytest.mark.parametrize("identity", [None, {"release": 1, "managed_by": None}])
def test_current_low_level_null_refused_and_explicit_identity_preserved(legacy, identity):
    from dataclasses import replace

    root, data, health, _, _ = legacy
    selected = replace(current(root), release=1)
    health["identity"] = identity
    path = data / "runtime/discovery.json"
    snapshot = path.read_bytes()
    backend = Backend(root, root / "install.log", root / "uv", process_control="strict")
    if identity is None:
        with pytest.raises(RuntimeError):
            backend.source_checkout_ownership(data, selected)
        with pytest.raises(ValueError):
            update_job._probe_selected_identity(data, selected, root, target_release=2)
    else:
        assert backend.source_checkout_ownership(data, selected) is False
        assert update_job._probe_selected_identity(data, selected, root, target_release=2) == (
            health["runtime_instance_id"], identity)
    assert path.read_bytes() == snapshot


def test_native_rechecks_legacy_tuple_and_discovery(legacy, monkeypatch):
    root, data, health, _, _ = legacy
    old = current(root)
    instance, identity = update_job._probe_selected_identity(data, old, root, target_release=1)
    assert (instance, identity) == (health["runtime_instance_id"], {"release": 0, "managed_by": None})
    health["schema_digest"] = "sha256:wrong"
    with pytest.raises(ValueError):
        update_job._probe_selected_identity(data, old, root, target_release=1)


def test_legacy_exception_requires_update_context(legacy):
    root, data, _, _, _ = legacy
    backend = Backend(root, root / "install.log", root / "uv")
    with pytest.raises(RuntimeError):
        backend.source_checkout_ownership(data, current(root))


def test_linux_r0_unscoped_birth_requires_exact_legacy_binding(legacy, monkeypatch):
    root, data, health, discovery, _ = legacy
    discovery["process_birth_id"] = "proc-start-ticks:19"
    write_json(data / "runtime/discovery.json", discovery)
    monkeypatch.setattr("runtime_protocol.platform.process_identity", lambda pid: {
        "pid": pid, "birth_id": SCOPED_R0_BIRTH})

    backend = gate(root, data)
    assert backend._legacy_identity_admitted

    # The unscoped tick cannot authorize a normal or foreign Runtime health.
    health["identity"] = {"release": 1, "managed_by": None}
    with pytest.raises(RuntimeError, match="startup identity cannot be verified"):
        gate(root, data)

    health.pop("identity")
    health["runtime_instance_id"] = "replacement"
    with pytest.raises(RuntimeError, match="startup identity cannot be verified"):
        gate(root, data)


@pytest.mark.parametrize("actual", [
    SCOPED_R0_BIRTH.replace("ticks:19", "ticks:20"),  # another incarnation
    SCOPED_R0_BIRTH.rsplit(";pidns:", 1)[0],  # incomplete scope
    SCOPED_R0_BIRTH + ";extra:value",  # unrecognized encoding
])
def test_linux_r0_unscoped_birth_refuses_nonmatching_or_partial_scope(legacy, monkeypatch, actual):
    root, data, _, discovery, _ = legacy
    discovery["process_birth_id"] = "proc-start-ticks:19"
    write_json(data / "runtime/discovery.json", discovery)
    monkeypatch.setattr("runtime_protocol.platform.process_identity", lambda pid: {
        "pid": pid, "birth_id": actual})

    with pytest.raises(RuntimeError, match="owner identity changed"):
        gate(root, data)


def test_linux_r0_unscoped_birth_does_not_bypass_update_context(legacy, monkeypatch):
    root, data, _, discovery, _ = legacy
    discovery["process_birth_id"] = "proc-start-ticks:19"
    write_json(data / "runtime/discovery.json", discovery)
    monkeypatch.setattr("runtime_protocol.platform.process_identity", lambda pid: {
        "pid": pid, "birth_id": SCOPED_R0_BIRTH})

    backend = Backend(root, root / "install.log", root / "uv")
    with pytest.raises(RuntimeError, match="startup identity cannot be verified"):
        backend.source_checkout_ownership(data, current(root))


@pytest.mark.parametrize("target, expected", [(1, 0), (2, 1)])
def test_cli_admits_only_reviewed_update_target(legacy, tmp_path, monkeypatch, target, expected):
    from unittest.mock import Mock
    from astrid.installer import cli
    from astrid.installer.service import Service
    from runtime_protocol import platform

    root, data, _, _, _ = legacy
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(Service, "recorded", lambda self: {})
    monkeypatch.setattr(Backend, "preflight_stop", lambda *args: None)
    monkeypatch.setattr(update_job, "sweep_owned_jobs", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "validate_manifest", lambda manifest: manifest)
    installer = Mock()
    monkeypatch.setattr(cli, "Installer", Mock(return_value=installer))
    manifest = tmp_path / "target.json"
    write_json(manifest, {"release": target})
    assert cli.main(["--update", "--root", str(root), "--data-root", str(data),
                     "--manifest", str(manifest)]) == expected
    assert installer.run.call_count == (1 if target == 1 else 0)


@pytest.mark.parametrize("request_identity", [None, {"release": 0, "managed_by": None}])
def test_native_acceptance_and_worker_binding_revalidate(legacy, tmp_path, monkeypatch, request_identity):
    from tests.test_installer_update_job import _fixture
    root, data, health, _, _ = legacy
    real_probe = update_job._probe_identity
    _, _, _, request = _fixture(tmp_path / "job-inputs", monkeypatch)
    import hashlib
    target = Path(request["metadata"]["manifest_path"])
    manifest = json.loads(target.read_text())
    manifest["release"] = 1
    target.write_text(json.dumps(manifest))
    request["metadata"].update(release=1, manifest_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
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
    update_job._probe_selected_identity(data, old, root, target_release=1)
    assert calls == [True, True, True, True]
    health["schema_digest"] = "sha256:wrong"
    assert not legacy_identity.admitted(old, health, discovery, program_root=root)
    assert not legacy_identity_observation(health, discovery, record, json.loads(raw), raw,
                                           old.root / "version.json", contract)[0]


def test_missing_shipped_table_fails_closed(legacy, monkeypatch):
    root, _, health, discovery, _ = legacy
    monkeypatch.setattr(legacy_identity, "_policy", lambda: None)
    assert not legacy_identity.admitted(current(root), health, discovery, program_root=root)


@pytest.mark.parametrize("content", [None, b"not json", b"{}", b'[{"action": "update", "target_release": 2}]',
                                    Path(__file__).parents[1].joinpath("astrid/installer/predecessors.json").read_bytes() + b" "])
def test_missing_or_malformed_resource_refuses(legacy, monkeypatch, content):
    root, _, health, discovery, _ = legacy
    class Resource:
        def joinpath(self, _name):
            return self
        def read_bytes(self):
            if content is None:
                raise FileNotFoundError()
            return content
    monkeypatch.setattr(legacy_identity.resources, "files", lambda _package: Resource())
    assert not legacy_identity.admitted(current(root), health, discovery, program_root=root)


@pytest.mark.parametrize("target", [None, 0, 2, True, "1"])
def test_unlisted_target_refused(legacy, target):
    root, data, _, _, _ = legacy
    assert not legacy_identity.allows_update(target)
    with pytest.raises(ValueError):
        update_job._probe_selected_identity(data, current(root), root, target_release=target)
