"""In-process declaration/managed-command transport; no media or Runtime store."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.execution.generic_host import (
    GenericPackHost, HostError, _ConfinementPolicy, _SandboxEnvironmentPolicy, _SandboxProjectScope,
    _capability_digest,
)
from astrid.packs.chiptune.actions._synth import parse_structured, validate_sections
from astrid.packs.chiptune.actions.compose.run import build_parser
from astrid.packs.chiptune.actions.sfx.run import build_parser as sfx_build_parser
from tests.test_generic_host import FakeRuntime, _write_manifest

PACK_ROOT = Path(__file__).resolve().parents[3] / "astrid/packs/chiptune"

def test_public_declaration_uses_admitted_required_default_without_consumer_guess(tmp_path):
    pack = tmp_path / "required_projection"
    pack.mkdir()
    (pack / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "required_projection", "name": "Required projection", "version": "1.0.0",
        "actions": {"probe": {"description": "Synthetic declaration only",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"]}},
            "inputs": [{"name": "implicit", "type": "string"},
                       {"name": "optional", "type": "string", "required": False}],
            "outputs": []}}}))
    host = GenericPackHost(pack_roots=[pack])
    record = host.discover()[0]
    actual = {port.name: port.required for port in record.definition.inputs}
    declaration = {port["name"]: port["required"] for port in record.public_action_declaration()["inputs"]}
    assert declaration == actual == {"implicit": True, "optional": False}
    assert record.capability_digest == _capability_digest(record.definition.to_dict())


@pytest.mark.parametrize("capability", ["editorial.boundary_candidates", "editorial.human_notes"])
def test_real_editorial_declarations_publish_required_input_booleans(capability):
    editorial = PACK_ROOT.parent / "editorial"
    host = GenericPackHost(pack_roots=[editorial])
    host.discover()
    record = host.capabilities[capability]
    public = record.public_action_declaration()
    assert public["inputs"]
    assert {v["name"]: v["required"] for v in public["inputs"]} == {
        port.name: port.required for port in record.definition.inputs}
    assert all(type(v["required"]) is bool for v in public["inputs"])



def _registered_host(tmp_path, monkeypatch, *, confinement_policy=None, pack_root=PACK_ROOT):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    legacy = tmp_path / "legacy"
    _write_manifest(legacy)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[pack_root, legacy], client=runtime,
                           attempt_base=tmp_path / "attempts",
                           confinement_policy=confinement_policy)
    host.discover()
    host.register()
    return host, runtime


def test_discovered_declarations_register_with_their_exact_definition(tmp_path, monkeypatch):
    host, runtime = _registered_host(tmp_path, monkeypatch)
    descriptors = {row["capability_id"]: row for row in runtime.registrations[0][1]["capabilities"]}
    for name in ("chiptune.compose", "chiptune.midi", "chiptune.sfx"):
        record = host.capabilities[name]
        row = descriptors[name]
        declaration = record.definition.metadata["action_declaration"]
        assert row["action_declaration"] == declaration
        assert row["definition_digest"] == record.capability_digest == _capability_digest(record.definition.to_dict())
        assert row["status"] == "ready"
        # Transport only the normalized declaration, not the executor metadata.
        assert "pack_root" not in row and "action_invocation" not in row
    compose = descriptors["chiptune.compose"]
    sfx = descriptors["chiptune.sfx"]
    assert compose["action_declaration"] != sfx["action_declaration"]
    assert compose["definition_digest"] != sfx["definition_digest"]
    assert "action_declaration" not in descriptors["test.echo"]
    assert runtime.capability_registrations == []  # One atomic registration.


@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("binding", ["inputs", "params"])
def test_managed_chiptune_task_preserves_declared_sections_until_launch(tmp_path, monkeypatch, count, binding):
    attempt = tmp_path / "attempts" / "sections-task-sections-attempt"
    # The binder probe stops at launch, but bootstrap preparation still requires
    # the same explicit attempt policy as a real command. Never bypass that guard.
    policy = _ConfinementPolicy(
        attempt, attempt / "outputs",
        _SandboxProjectScope(attempt / "inputs", (attempt / "inputs",)),
        bootstrap_files=(), environment=_SandboxEnvironmentPolicy(),
        bootstrap_roots=(PACK_ROOT, PACK_ROOT.parents[1]),
    )
    host, runtime = _registered_host(tmp_path, monkeypatch, confinement_policy=policy)
    record = host.capabilities["chiptune.compose"]
    sections_port = next(port for port in record.definition.inputs if port.name == "sections")
    assert sections_port.type == "json" and sections_port.required
    mapping = next(item for item in record.definition.command.input_args if item.input == "sections")
    assert not mapping.repeatable
    sections = [{"start_s": index, "end_s": index + 1, "energy": 0.6, "mood": "bright"}
                for index in range(count)]
    captured = []

    def stop_before_launch(argv, **kwargs):
        # The real managed task path and command binder have run; the process
        # boundary is stopped deliberately before any synthesis or settlement.
        assert host.managed_tool_session.occupied
        assert argv[1:5] == ["-I", "-S", "-B", "-c"] and len(argv) == 8
        assert str(PACK_ROOT) in json.loads(argv[6])
        command = json.loads(argv[7])
        assert command[:2] == ["-m", "astrid.packs.chiptune.actions.compose.run"]
        assert command.count("--sections") == 1
        args = build_parser().parse_args(command[2:])
        assert args.duration_s == 2
        parsed = parse_structured(args.sections, "sections")
        assert json.loads(args.sections) == parsed == sections
        if count:
            assert len(validate_sections(parsed, args.duration_s)) == count
        else:
            with pytest.raises(AstridError, match="non-empty list"):
                validate_sections(parsed, args.duration_s)
        captured.append(parsed)
        raise HostError("in-process stop before synthesis")

    monkeypatch.setattr(host, "_workload_launch_plan", stop_before_launch)
    task = {"task": {"id": "sections-task", "capability": record.id,
                     "project_id": "unit-project", "attempt_id": "sections-attempt", "fence": 1,
                     "spec": {"spec": {"family": record.id, "output_policy": {},
                                       binding: {"duration_s": 2, "sections": sections}}}}}
    if binding == "params" and count == 1:
        task["task"]["spec"]["spec"]["inputs"] = {"duration_s": 2}
    runtime.tasks["sections-task"] = task
    with pytest.raises(HostError, match="in-process stop before synthesis"):
        host.run_task(task, lease_token="unit-lease")
    assert captured == [sections]
    assert len(runtime.failures) == 1
    assert runtime.settlements == [] and runtime.uploaded_objects == {}
    assert not host.managed_tool_session.occupied


def test_other_registered_command_binds_canonical_params(tmp_path, monkeypatch):
    attempt = tmp_path / "attempts" / "sfx-task-sfx-attempt"
    policy = _ConfinementPolicy(
        attempt, attempt / "outputs",
        _SandboxProjectScope(attempt / "inputs", (attempt / "inputs",)),
        bootstrap_files=(), environment=_SandboxEnvironmentPolicy(),
        bootstrap_roots=(PACK_ROOT, PACK_ROOT.parents[1]),
    )
    host, runtime = _registered_host(tmp_path, monkeypatch, confinement_policy=policy)
    captured = []

    def stop_before_launch(argv, **kwargs):
        command = json.loads(argv[7])
        assert command[:2] == ["-m", "astrid.packs.chiptune.actions.sfx.run"]
        args = sfx_build_parser().parse_args(command[2:])
        assert args.kind == "coin" and args.duration_s == 0.25
        captured.append(args)
        raise HostError("in-process stop before synthesis")

    monkeypatch.setattr(host, "_workload_launch_plan", stop_before_launch)
    task = {"task": {"id": "sfx-task", "capability": "chiptune.sfx",
                     "project_id": "unit-project", "attempt_id": "sfx-attempt", "fence": 1,
                     "spec": {"spec": {"family": "chiptune.sfx", "output_policy": {},
                                       "params": {"kind": "coin", "duration_s": 0.25}}}}}
    runtime.tasks["sfx-task"] = task
    with pytest.raises(HostError, match="in-process stop before synthesis"):
        host.run_task(task, lease_token="unit-lease")
    assert len(captured) == len(runtime.failures) == 1
    assert runtime.settlements == [] and runtime.uploaded_objects == {}
    assert not host.managed_tool_session.occupied


@pytest.mark.parametrize("spec,error", [
    ({"params": {"duration_s": 2, "unknown": True}}, "undeclared parameter.*unknown"),
    ({"params": {"duration_s": 2}, "inputs": {"duration_s": 3}}, "conflicts with input binding: duration_s"),
    ({"params": None}, "params must be an object"),
    ({"params": []}, "params must be an object"),
])
def test_registered_command_rejects_invalid_params(tmp_path, monkeypatch, spec, error):
    host, runtime = _registered_host(tmp_path, monkeypatch)
    task = {"task": {"id": "invalid-task", "capability": "chiptune.compose",
                     "project_id": "unit-project", "attempt_id": "invalid-attempt", "fence": 1,
                     "spec": {"spec": spec}}}
    runtime.tasks["invalid-task"] = task
    monkeypatch.setattr(host, "_workload_launch_plan", lambda *a, **kw: pytest.fail("invalid params reached launch"))
    with pytest.raises(HostError, match=error):
        host.run_task(task, lease_token="unit-lease")
    assert len(runtime.failures) == 1
    assert runtime.settlements == [] and runtime.uploaded_objects == {}


@pytest.mark.parametrize("ports,rejected", [([], "duration_s"), (["duration_s"], "sections")])
def test_registered_command_explicit_param_ports_win(tmp_path, monkeypatch, ports, rejected):
    pack_root = tmp_path / "chiptune"
    shutil.copytree(PACK_ROOT, pack_root)
    manifest = pack_root / "pack.yaml"
    payload = json.loads(manifest.read_text())
    payload["actions"]["compose"]["metadata"]["hc04_param_ports"] = ports
    manifest.write_text(json.dumps(payload))
    host, runtime = _registered_host(tmp_path, monkeypatch, pack_root=pack_root)
    task = {"task": {"id": "explicit-task", "capability": "chiptune.compose",
                     "project_id": "unit-project", "attempt_id": "explicit-attempt", "fence": 1,
                     "spec": {"spec": {"params": {"duration_s": 2, "sections": []}}}}}
    runtime.tasks["explicit-task"] = task
    with pytest.raises(HostError, match=f"undeclared parameter.*{rejected}"):
        host.run_task(task, lease_token="unit-lease")
    assert len(runtime.failures) == 1
    assert runtime.settlements == [] and runtime.uploaded_objects == {}
