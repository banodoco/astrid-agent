"""In-process declaration/managed-command transport; no media or Runtime store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.execution.generic_host import GenericPackHost, HostError, _capability_digest
from astrid.packs.chiptune.actions._synth import parse_structured, validate_sections
from astrid.packs.chiptune.actions.compose.run import build_parser
from tests.test_generic_host import FakeRuntime, _write_manifest

PACK_ROOT = Path(__file__).resolve().parents[3] / "astrid/packs/chiptune"


def _registered_host(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    legacy = tmp_path / "legacy"
    _write_manifest(legacy)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[PACK_ROOT, legacy], client=runtime,
                           attempt_base=tmp_path / "attempts")
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
def test_managed_chiptune_task_preserves_declared_sections_until_launch(tmp_path, monkeypatch, count):
    host, runtime = _registered_host(tmp_path, monkeypatch)
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
        assert argv[1:3] == ["-m", "astrid.packs.chiptune.actions.compose.run"]
        assert argv.count("--sections") == 1
        args = build_parser().parse_args(argv[3:])
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
                     "spec": {"spec": {"inputs": {"duration_s": 2, "sections": sections}}}}}
    runtime.tasks["sections-task"] = task
    with pytest.raises(HostError, match="in-process stop before synthesis"):
        host.run_task(task, lease_token="unit-lease")
    assert captured == [sections]
    assert len(runtime.failures) == 1
    assert runtime.settlements == [] and runtime.uploaded_objects == {}
    assert not host.managed_tool_session.occupied
