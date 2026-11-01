"""D40 joins the real private producer to the existing host and source loaders."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as host_module
from astrid.core.execution import generic_host_worker as worker
from tests.core.execution.test_generic_host_actions_v3 import pack


def _runtime_fixture(name):
    # Reuse the producer's owner fixture without replacing Astrid's test package.
    import runtime_protocol

    import tests

    directory = str(Path(runtime_protocol.__file__).resolve().parent.parent / "tests")
    if directory not in tests.__path__:
        tests.__path__.append(directory)
    return __import__("tests." + name, fromlist=[name])


def _repository(root):
    git = root.parent / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "config").write_text('[core]\nrepositoryformatversion = 0\nbare = false\n[remote "origin"]\nurl = https://example.test/owner/tool.git\n')

    def obj(kind, data):
        raw = kind.encode() + b" " + str(len(data)).encode() + b"\0" + data
        pin = hashlib.sha1(raw).hexdigest()
        path = git / "objects" / pin[:2] / pin[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(raw))
        return pin

    def tree(directory):
        entries = []
        for path in sorted(directory.iterdir()):
            mode = b"40000" if path.is_dir() else b"100644"
            pin = tree(path) if path.is_dir() else obj("blob", path.read_bytes())
            entries.append(mode + b" " + path.name.encode() + b"\0" + bytes.fromhex(pin))
        return obj("tree", b"".join(entries))

    subtree = tree(root)
    top = obj("tree", b"40000 " + root.name.encode() + b"\0" + bytes.fromhex(subtree))
    revision = obj("commit", f"tree {top}\nauthor Owner <o@example.test> 1 +0000\ncommitter Owner <o@example.test> 1 +0000\n\nfixture\n".encode())
    (git / "refs/heads/main").write_text(revision + "\n")
    return revision, subtree


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    from runtime_protocol import external_pack_sources as producer
    from runtime_protocol import verified_host_launch as gate
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    monkeypatch.setattr(host_module, "_runtime_external_sources", None)
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    root, host, _, definition, admission = pack(tmp_path)
    # A relative import verifies that descendants use the same byte loader.
    (root / "actions/hello.py").write_text("from .helper import answer\ndef run(value='default'):\n    return answer\n")
    (root / "actions/helper.py").write_text("answer = 'captured'\n")
    (root / "emit.py").write_text("from .actions.helper import answer\nassert __file__.endswith('emit.py')\nprint(answer)\n")
    (root / "__main__.py").write_text("from . import emit\n")
    if getattr(request, "param", "python") == "command":
        import yaml

        path = root / "pack.yaml"
        manifest = yaml.safe_load(path.read_text())
        action = manifest["actions"]["calculate"]
        action["inputs"] = []
        action["invocation"] = {"kind": "command", "command": {
            "argv": ["{python_exec}", "-m", "unrelated.emit"]}}
        path.write_text(yaml.safe_dump(manifest))
    host.discover()
    definition, admission = host.admit("executor", "unrelated.calculate")
    record = host.capabilities["unrelated.calculate"]
    revision, tree = _repository(root)
    f = _runtime_fixture("test_verified_host_launch").fixture_authority(tmp_path / "authority")
    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm, support_root=tmp_path / "support")
    grant = {"repository": "https://example.test/owner/tool.git", "checkout": str(root.parent),
             "revision": revision, "subpath": root.name, "tree": tree, "root": str(root),
             "files": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in root.rglob("*") if p.is_file()},
             "definitions": [{"path": "pack.yaml", "capability_kind": "executor",
                              "capability_id": record.id, "definition_digest": record.capability_digest}]}
    f.args["config"]["support_root"] = str(service.support_root)
    preparation = f.args["preparation"]
    preparation["profile"].update(workspace_uuid=service.realm["id"], support_root=str(service.support_root), host_launch={
        "additional_pack_roots": [{"path": str(root), "files": grant["files"]}]})
    owner = _runtime_fixture("test_remote_worker_activation").OWNER
    producer._write_owner_grants(service, [grant], identity=owner)
    capture = producer.OwnerSourceCapture(preparation=preparation, config=f.args["config"])
    selected = gate._RuntimeExternalSources(capture.payload)
    yield SimpleNamespace(root=root, host=host, definition=definition, admission=admission,
                          record=record, capture=capture, selected=selected, preparation=preparation,
                          producer=producer, gate=gate, grant=grant, service=service, owner=owner)
    service.store.close()


def _bind(case):
    host_module._consume_runtime_external_sources(sources=case.selected)
    custody = host_module._runtime_external_sources
    custody.bind(case.preparation)
    return custody


def test_real_producer_calls_consumer_before_cli_and_requires_retained_preparation(case, monkeypatch):
    calls = []
    def cli():
        calls.append("cli")
        with pytest.raises(host_module.HostError, match="retained preparation"):
            case.host.discover()
        _bind_existing = host_module._runtime_external_sources
        _bind_existing.bind(case.preparation)
        assert case.host.discover()[0].id == case.record.id
    monkeypatch.setattr(host_module, "_cli", cli)
    with pytest.raises(SystemExit):
        case.gate._run_external_host(host_module, case.capture.payload, ["run"])
    assert calls == ["cli"]


@pytest.mark.parametrize("edge", ["scope", "profile", "definition", "revision", "root", "bytes", "ancestor", "symlink"])
def test_mutation_before_discovery_or_selection_fails_closed(case, edge):
    custody = _bind(case)
    if edge == "scope":
        foreign = copy.deepcopy(case.preparation)
        foreign["owner_epoch"] = "foreign"
        with pytest.raises(host_module.HostError):
            custody.bind(foreign)
        return
    if edge == "profile":
        foreign = copy.deepcopy(case.preparation)
        foreign["profile"]["workspace_uuid"] = "foreign"
        with pytest.raises(host_module.HostError):
            custody.bind(foreign)
        return
    if edge == "definition":
        record = copy.copy(case.record)
        object.__setattr__(record, "capability_digest", "sha256:" + "f" * 64)
        with pytest.raises(host_module.HostError):
            custody.select(record)
        return
    if edge == "revision":
        (case.root.parent / ".git/refs/heads/main").write_text("f" * 40 + "\n")
    elif edge == "root":
        case.root.rename(case.root.with_name("moved"))
        case.root.mkdir()
    elif edge == "ancestor":
        parent = case.root.parent
        parent.rename(parent.with_name("moved-parent"))
        parent.mkdir()
    elif edge == "symlink":
        source = case.root / "actions/helper.py"
        source.rename(source.with_name("saved.py"))
        source.symlink_to(source.with_name("saved.py"))
    else:
        (case.root / "actions/helper.py").write_text("raise AssertionError('live bytes')\n")
    with pytest.raises((host_module.HostError, case.gate.HostLaunchError, OSError)):
        case.host.discover()


def test_selection_rejects_caller_handles_and_duplicate_consumption(case):
    forged = SimpleNamespace(inventory=lambda: case.capture.payload, select=case.selected.select)
    with pytest.raises(host_module.HostError, match="private Runtime"):
        host_module._consume_runtime_external_sources(sources=forged)
    _bind(case)
    with pytest.raises(host_module.HostError, match="already consumed"):
        host_module._consume_runtime_external_sources(sources=case.selected)


@pytest.mark.parametrize("name", ["astrid.py", "runtime_protocol.py", "astrid_runtime_client.py"])
def test_owner_grants_cannot_authorize_package_collisions(case, name):
    grant = copy.deepcopy(case.grant)
    grant["files"][name] = "a" * 64
    with pytest.raises(case.gate.HostLaunchError, match="collision"):
        case.producer._write_owner_grants(case.service, [grant], identity=case.owner)


def test_capture_after_bind_mutation_is_rejected_before_execution(case):
    custody = _bind(case)
    (case.root / "actions/helper.py").write_text("raise AssertionError('live')\n")
    with pytest.raises((host_module.HostError, case.gate.HostLaunchError)):
        custody.descriptor(case.record)


def test_command_loader_executes_captured_descendants_after_last_check(case, capsys):
    custody = _bind(case)
    with custody.descriptor(case.record) as channel:
        # The child receives only the inherited handle, not task/admission JSON.
        selected, contents = worker._read_captured_source(os.dup(channel.fileno()))
    (case.root / "actions/helper.py").write_text("raise AssertionError('live child source')\n")
    assert worker._run_captured_command(json.dumps(["-m", "unrelated.emit"]), selected, contents) == 0
    assert capsys.readouterr().out == "captured\n"


def test_python_action_loader_executes_captured_descendants_after_last_check(case, monkeypatch):
    from astrid.core.execution.executor import runner
    from astrid.core.execution.executor.registry import ExecutorRegistry
    from astrid.core.execution.executor.schema import validate_executor_definition

    custody = _bind(case)
    with custody.descriptor(case.record) as channel:
        _, contents = worker._read_captured_source(os.dup(channel.fileno()))
    original = runner._PackSourceLoader.exec_module
    mutated = []
    def mutate_then_load(self, module):
        if not mutated:
            (case.root / "actions/helper.py").write_text("raise AssertionError('live action source')\n")
            mutated.append(True)
        return original(self, module)
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setattr(runner._PackSourceLoader, "exec_module", mutate_then_load)
    result = runner.run_executor(runner.ExecutorRunRequest(executor_id=case.record.id, out=str(case.root.parent / "action-output"),
        project="demo", project_was_auto_resolved=True, invocation="runtime"),
        ExecutorRegistry([validate_executor_definition(case.definition)]),
        _admission=case.admission, _captured_source=contents)
    assert result.ok, result.error
    assert result.payload["action_result"] == "captured"
    assert mutated == [True]


@pytest.mark.parametrize("mutation", [False, True])
def test_production_python_action_uses_private_descriptor(case, tmp_path, monkeypatch, mutation):
    from tests.core.execution.test_generic_host_actions_v3 import invoke

    _bind(case)
    original = host_module.spawn_task_scope
    def launch(argv, **kwargs):
        assert "--external-source-fd" in argv
        assert "-I" in argv
        if mutation:
            (case.root / "actions/helper.py").write_text("raise AssertionError('post-bind live code')\n")
        return original(argv, **kwargs)
    monkeypatch.setattr(host_module, "spawn_task_scope", launch)
    if mutation:
        with pytest.raises(host_module.HostError, match="source digest changed"):
            invoke(tmp_path, case.host, case.definition, case.admission)
    else:
        result = invoke(tmp_path, case.host, case.definition, case.admission)
        assert result.payload["action_result"] == "captured"


@pytest.mark.parametrize("case", ["command"], indirect=True)
@pytest.mark.parametrize("mutation", [False, True])
def test_production_command_executes_only_capture_after_last_check(case, tmp_path, monkeypatch, mutation):
    _bind(case)
    original = host_module.spawn_task_scope
    def launch(argv, **kwargs):
        assert "--external-source-fd" in argv and "-I" in argv
        if mutation:
            (case.root / "actions/helper.py").write_text("raise AssertionError('live command source')\n")
            (case.root / "astrid").mkdir()
            (case.root / "astrid/__init__.py").write_text("raise AssertionError('cwd shadow')\n")
        return original(argv, **kwargs)
    monkeypatch.setattr(host_module, "spawn_task_scope", launch)
    attempt = tmp_path / "attempt"
    outputs = attempt / "outputs"
    outputs.mkdir(parents=True)
    result = case.host._run_command_definition(case.record, {}, outputs, attempt,
        admission=case.admission)
    assert result.returncode == 0


def test_revoked_owner_custody_never_becomes_a_source_grant(case):
    custody = _bind(case)
    case.producer._write_owner_grants(case.service, [], identity=case.owner)
    with pytest.raises(host_module.HostError, match="revoked"):
        custody.descriptor(case.record)


def test_source_mutation_before_consumer_is_rejected(case):
    (case.root / "actions/helper.py").write_text("raise AssertionError('before discovery')\n")
    with pytest.raises((host_module.HostError, case.gate.HostLaunchError)):
        host_module._consume_runtime_external_sources(sources=case.selected)


@pytest.mark.parametrize("payload", [b"{}", b"null", b"[]", b"{broken", b"42"])
def test_malformed_capture_never_reaches_discovery(case, payload):
    with pytest.raises((host_module.HostError, case.gate.HostLaunchError)):
        host_module._consume_runtime_external_sources(sources=case.gate._RuntimeExternalSources(payload))
    assert host_module._runtime_external_sources is None


def test_wrong_source_root_cannot_fall_back_to_live_execution(case):
    custody = _bind(case)
    record = copy.copy(case.record)
    object.__setattr__(record, "source_root", case.root.parent / "foreign")
    with pytest.raises(host_module.HostError, match="no Runtime owner grant"):
        custody.descriptor(record)


def test_task_claims_cannot_write_runtime_owner_grants(case, monkeypatch):
    from runtime_protocol.errors import AuthorizationError

    before = case.capture.payload
    monkeypatch.setenv("PYTHONPATH", str(case.root))
    with pytest.raises(AuthorizationError):
        case.producer._write_owner_grants(case.service, [case.grant],
            identity={"actor": "executor:host", "scopes": ["admin"]})
    assert case.capture.payload == before


def test_package_main_uses_captured_loader(case, capsys):
    custody = _bind(case)
    with custody.descriptor(case.record) as channel:
        selected, contents = worker._read_captured_source(os.dup(channel.fileno()))
    (case.root / "__main__.py").write_text("raise AssertionError('live main')\n")
    assert worker._run_captured_command(json.dumps(["-m", "unrelated"]), selected, contents) == 0
    assert capsys.readouterr().out == "captured\n"


def test_captured_command_refuses_installed_namespace_collision(case):
    custody = _bind(case)
    with custody.descriptor(case.record) as channel:
        selected, contents = worker._read_captured_source(os.dup(channel.fileno()))
    contents["json.py"] = b"raise AssertionError('installed collision')\n"
    with pytest.raises(ValueError, match="installed package"):
        worker._run_captured_command(json.dumps(["-m", "unrelated.emit"]), selected, contents)


def test_standalone_runtime_launcher_keeps_its_own_private_handle_class(case, monkeypatch):
    # Production executes this stdlib-only producer as a standalone script,
    # so its class differs from the subsequently imported Runtime module.
    name = "_d40_standalone_runtime_launcher"
    spec = importlib.util.spec_from_file_location(name, case.gate.__file__)
    launcher = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, launcher)
    spec.loader.exec_module(launcher)
    assert launcher._RuntimeExternalSources is not case.gate._RuntimeExternalSources
    host_module._consume_runtime_external_sources(
        sources=launcher._RuntimeExternalSources(case.capture.payload))
    host_module._runtime_external_sources.bind(case.preparation)
    assert case.host.discover()[0].id == case.record.id
