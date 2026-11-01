"""The existing orchestrator consumer must retain Runtime's private source custody."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as host_module
from tests.core.execution.test_runtime_source_custody_d40 import _repository, _runtime_fixture


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    from runtime_protocol import external_pack_sources as producer
    from runtime_protocol import verified_host_launch as gate
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    monkeypatch.setattr(host_module, "_runtime_external_sources", None)
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    root = tmp_path / "external_pack"
    folder = root / "orchestrators/demo"
    folder.mkdir(parents=True)
    (root / "external_pkg").mkdir()
    (root / "pack.yaml").write_text(
        "schema_version: 2\nid: external_pack\nname: External Pack\nversion: '1.0'\n"
        "status: active\ndocumentation:\n  kind: none\n  reason: test fixture\n"
        "content:\n  orchestrators: orchestrators\n")
    (folder / "orchestrator.yaml").write_text(
        "schema_version: 1\nid: external_pack.demo\nname: Demo\nkind: external\nversion: '1.0'\n"
        f"runtime:\n  kind: python\n  module: {getattr(request, 'param', 'external_pkg.run')}\n  function: run\n")
    (root / "external_pkg/__init__.py").write_text("")
    (root / "external_pkg/helper.py").write_text("answer = 'captured'\n")
    (root / "external_pkg/run.py").write_text(
        "import os\nfrom .helper import answer\n"
        "def run(request, orchestrator):\n"
        "    return {'returncode': 0, 'outputs': {'answer': answer, 'imported_from': os.path.dirname(__file__)}}\n")
    host = host_module.GenericPackHost(pack_roots=[root])
    # Calculate the exact existing folder semantics before owner capture.
    definition, admission = host.admit("orchestrator", "external_pack.demo")
    revision, tree = _repository(root)
    f = _runtime_fixture("test_verified_host_launch").fixture_authority(tmp_path / "authority")
    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm, support_root=tmp_path / "support")
    grant = {"repository": "https://example.test/owner/tool.git", "checkout": str(root.parent),
             "revision": revision, "subpath": root.name, "tree": tree, "root": str(root),
             "files": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in root.rglob("*") if p.is_file()},
             "definitions": [{"path": "orchestrators/demo/orchestrator.yaml", "capability_kind": "orchestrator",
                              "capability_id": "external_pack.demo", "definition_digest": admission["capability_digest"]}]}
    f.args["config"]["support_root"] = str(service.support_root)
    preparation = f.args["preparation"]
    preparation["profile"].update(workspace_uuid=service.realm["id"], support_root=str(service.support_root), host_launch={
        "additional_pack_roots": [{"path": str(root), "files": grant["files"]}]})
    owner = _runtime_fixture("test_remote_worker_activation").OWNER
    producer._write_owner_grants(service, [grant], identity=owner)
    capture = producer.OwnerSourceCapture(preparation=preparation, config=f.args["config"])
    selected = gate._RuntimeExternalSources(capture.payload)
    host_module._consume_runtime_external_sources(sources=selected)
    custody = host_module._runtime_external_sources
    custody.bind(preparation)
    yield SimpleNamespace(root=root, host=host, definition=definition, admission=admission,
                          custody=custody, preparation=preparation, gate=gate,
                          producer=producer, service=service, grant=grant, owner=owner, config=f.args["config"])
    service.store.close()


def invoke(case, tmp_path, **overrides):
    definition, admission = case.host.admit("orchestrator", "external_pack.demo")
    assert definition == case.definition
    assert admission == case.admission
    return case.host.invoke_capability(
        capability_kind="orchestrator", capability_id="external_pack.demo",
        request={"orchestrator_id": "external_pack.demo", "project": "demo",
                 "project_was_auto_resolved": True, "out": None, "run_root": str(tmp_path / "attempt")},
        attempt=tmp_path / "attempt", definition=overrides.get("definition", definition), admission=admission)


def test_existing_orchestrator_invocation_uses_owner_capture(case, tmp_path, monkeypatch):
    from astrid.core.execution.orchestrator import folder

    def live_discovery(path):
        pytest.fail("captured manifests must not use the live folder loader")
    monkeypatch.setattr(folder, "load_folder_orchestrator", live_discovery)
    result = invoke(case, tmp_path)
    assert result.ok
    assert result.returncode == 0
    assert result.outputs["answer"] == "captured"
    assert str(case.root / "external_pkg") in result.outputs["imported_from"]


@pytest.mark.parametrize("edge", ["custody_scope", "owner_epoch", "channel_id", "operation_id", "profile"])
def test_wrong_retained_authority_refused_before_discovery(case, monkeypatch, edge):
    preparation = copy.deepcopy(case.preparation)
    if edge == "profile":
        preparation["profile"]["workspace_uuid"] = "foreign"
    else:
        preparation[edge] = "foreign"
    with pytest.raises(host_module.HostError, match="retained preparation"):
        case.custody.bind(preparation)


@pytest.mark.parametrize("edge", ["revision", "definition", "path", "inventory", "revoked"])
def test_wrong_source_authority_refused_before_execution(case, tmp_path, monkeypatch, edge):
    launches = []
    monkeypatch.setattr(host_module, "spawn_task_scope", lambda *a, **kw: launches.append(a))
    if edge == "revision":
        (case.root.parent / ".git/refs/heads/main").write_text("f" * 40 + "\n")
    elif edge == "path":
        (case.root / "external_pkg/run.py").write_text("raise AssertionError('live source')\n")
    elif edge == "inventory":
        monkeypatch.setattr(type(case.custody._sources), "inventory", lambda self: b"{}")
    elif edge == "revoked":
        case.producer._write_owner_grants(case.service, [], identity=case.owner)
    definition = copy.deepcopy(case.definition)
    if edge == "definition":
        definition["runtime"]["function"] = "foreign"
    with pytest.raises((host_module.HostError, case.gate.HostLaunchError)):
        invoke(case, tmp_path, definition=definition)
    assert launches == []


def test_capture_survives_last_check_to_import_mutation(case, tmp_path, monkeypatch):
    import os

    from astrid.core.execution import generic_host_worker as worker
    from astrid.core.execution.orchestrator import runner
    from astrid.core.execution.orchestrator.registry import OrchestratorRegistry
    from astrid.core.execution.orchestrator.schema import validate_orchestrator_definition

    definition, admission = case.host.admit("orchestrator", "external_pack.demo")
    record = case.host._orchestrators["external_pack.demo"]
    with case.custody.descriptor(record, capability_kind="orchestrator") as channel:
        captured = worker._read_captured_source(os.dup(channel.fileno()))
    (case.root / "external_pkg/helper.py").write_text("raise AssertionError('live import')\n")
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    result = runner.run_orchestrator(
        runner.OrchestratorRunRequest(orchestrator_id="external_pack.demo", project="demo",
            project_was_auto_resolved=True, run_root=tmp_path / "captured-attempt"),
        OrchestratorRegistry([validate_orchestrator_definition(definition)]), _captured_source=captured)
    assert result.ok
    assert result.outputs["answer"] == "captured"


def test_ungranted_orchestrator_never_reaches_dynamic_discovery(case, tmp_path, monkeypatch):
    from astrid.core.execution.orchestrator import folder

    root = tmp_path / "ungranted"
    root.mkdir()
    (root / "orchestrator.py").write_text("raise AssertionError('ungranted discovery')\n")
    loads = []
    monkeypatch.setattr(folder, "load_folder_orchestrator", lambda path: loads.append(path))
    with pytest.raises(host_module.HostError, match="outside retained Runtime source custody"):
        host = host_module.GenericPackHost(pack_roots=[root])
        host.admit("orchestrator", "ungranted.demo")
    assert loads == []


def test_owner_grant_for_wrong_definition_is_refused_before_execution(case, tmp_path, monkeypatch):
    grant = copy.deepcopy(case.grant)
    grant["definitions"][0]["definition_digest"] = "sha256:" + "f" * 64
    case.producer._write_owner_grants(case.service, [grant], identity=case.owner)
    capture = case.producer.OwnerSourceCapture(preparation=case.preparation, config=case.config)
    monkeypatch.setattr(host_module, "_runtime_external_sources", None)
    host_module._consume_runtime_external_sources(sources=case.gate._RuntimeExternalSources(capture.payload))
    host_module._runtime_external_sources.bind(case.preparation)
    launches = []
    monkeypatch.setattr(host_module, "spawn_task_scope", lambda *a, **kw: launches.append(a))
    with pytest.raises(host_module.HostError, match="exact Runtime selection"):
        invoke(case, tmp_path)
    assert launches == []


@pytest.mark.parametrize("case", ["json"], indirect=True)
def test_selected_module_cannot_fall_back_to_installed_source(case, tmp_path):
    with pytest.raises(host_module.HostError, match="outside captured source"):
        invoke(case, tmp_path)
