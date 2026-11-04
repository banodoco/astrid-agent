"""Finite controls for the canonical schema consumer; no native launches."""

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

from astrid.core import timeline
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering import _local_schema, remotion_runtime
from astrid.core.timeline import banodoco_schema
from astrid.sdk import host_bootstrap


@pytest.fixture
def staged_schema(tmp_path):
    root = tmp_path / "agent"
    for relative in (_local_schema.SCHEMA_RELATIVE_PATH, Path("runtime.lock.json"),
                     *_local_schema.READER_INPUTS):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / relative).read_bytes())
    return root, root / _local_schema.SCHEMA_RELATIVE_PATH


def test_local_schema_refuses_foreign_origin_even_with_identical_bytes(staged_schema, tmp_path):
    root, schema = staged_schema
    foreign = tmp_path / schema.name
    foreign.write_bytes(schema.read_bytes())
    with pytest.raises(ValueError, match="origin differs"):
        _local_schema.verified_schema_path(root, str(foreign))


@pytest.mark.parametrize("change", ["digest", "pin", "source", "mutation", "missing", "symlink"])
def test_local_schema_refuses_bad_integrity(staged_schema, tmp_path, change):
    root, schema = staged_schema
    lock_path = root / "runtime.lock.json"
    lock = json.loads(lock_path.read_text())
    pin = lock["files"][_local_schema.SCHEMA_RELATIVE_PATH.as_posix()]
    if change == "digest":
        pin["sha256"] = "sha256:" + "0" * 64
    elif change == "pin":
        lock["files"] = {}
    elif change == "source":
        pin["from"] = "unapproved/schema.json"
    elif change == "mutation":
        schema.write_text("{}")
    elif change == "missing":
        # A new selected source with no canonical contract, not an ambient fallback.
        root = tmp_path / "missing"
        root.mkdir()
        schema = root / _local_schema.SCHEMA_RELATIVE_PATH
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
        schema = root / _local_schema.SCHEMA_RELATIVE_PATH
    lock_path.write_text(json.dumps(lock))
    with pytest.raises((OSError, ValueError)):
        _local_schema.verified_schema_path(root, str(schema))


@pytest.mark.parametrize("target", ["schema", "lock", "parent"])
def test_local_schema_refuses_writable_inputs(staged_schema, target):
    root, schema = staged_schema
    path = {"schema": schema, "lock": root / "runtime.lock.json", "parent": schema.parent}[target]
    path.chmod(0o777)
    with pytest.raises(ValueError, match="owner/mode"):
        _local_schema.verified_schema_path(root, str(schema))


def test_local_schema_refuses_foreign_owner(staged_schema, monkeypatch):
    root, schema = staged_schema
    monkeypatch.setattr(_local_schema, "owned_by_current_user", lambda *_args, **_kw: False)
    with pytest.raises(ValueError, match="owner/mode"):
        _local_schema.verified_schema_path(root, str(schema))


def test_bootstrap_refuses_mutated_staged_contract(staged_schema):
    root, schema = staged_schema
    schema.write_text("{}")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="Runtime source pin"):
        host_bootstrap._provision_render_runtime_env(root, {})


@pytest.mark.parametrize("change", ["origin", "cache"])
def test_canonical_reader_refuses_foreign_or_stale_contract(monkeypatch, change):
    schema = REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH
    if change == "origin":
        monkeypatch.setattr(banodoco_schema, "__file__", "/foreign/banodoco_schema.py")
    else:
        monkeypatch.setattr(banodoco_schema, "load_vendored_timeline_schema", lambda: {})
    with pytest.raises(ValueError, match="foreign origin|stale contract"):
        _local_schema.load_verified_schema(REPO_ROOT, str(schema))


@pytest.mark.parametrize("change", ["foreign_ref", "missing_ref", "invalid_schema", "nonfinite"])
def test_reader_rejects_invalid_schema_and_reference_closure(staged_schema, monkeypatch, change):
    root, schema_path = staged_schema
    schema = json.loads(schema_path.read_text())
    if change == "foreign_ref":
        schema["definitions"]["Unused"] = {"$ref": "https://invalid.example/schema.json"}
    elif change == "missing_ref":
        schema["definitions"]["Unused"] = {"$ref": "#/definitions/Absent"}
    elif change == "invalid_schema":
        schema["properties"]["clips"]["type"] = "not-a-json-type"
    else:
        schema["maximum"] = float("nan")
    schema_path.write_text(json.dumps(schema))
    lock_path = root / "runtime.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["files"][_local_schema.SCHEMA_RELATIVE_PATH.as_posix()]["sha256"] = (
        "sha256:" + hashlib.sha256(schema_path.read_bytes()).hexdigest())
    lock_path.write_text(json.dumps(lock))
    # Execute the unchanged, real canonical reader from this selected source.
    # No substitute model module or schema implementation is manufactured.
    reader_path = root / "astrid/core/timeline/banodoco_schema.py"
    spec = importlib.util.spec_from_file_location("selected_schema_reader", reader_path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    monkeypatch.setattr(timeline, "banodoco_schema", reader)
    with pytest.raises((ValueError, KeyError, jsonschema.SchemaError)):
        _local_schema.load_verified_schema(root, str(schema_path))


def test_strict_probe_consumes_the_selected_canonical_reader(monkeypatch, capsys):
    schema = REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH
    monkeypatch.setattr(sys, "argv", ["probe", str(schema), str(REPO_ROOT), "source"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(compile(remotion_runtime._schema_probe_source(), "<schema-probe>", "exec"), {})
    closure = json.loads(capsys.readouterr().out)
    assert closure["origin"] == str(schema)
    assert str(Path(_local_schema.__file__)) in closure["module_files"]
    assert str(Path(banodoco_schema.__file__)) in closure["module_files"]
    assert closure["startup"]["executable"] == sys.executable


def test_installed_probe_refuses_a_nonisolated_interpreter(monkeypatch):
    assert not sys.flags.isolated
    schema = REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH
    monkeypatch.setattr(sys, "argv", ["probe", str(schema), str(REPO_ROOT), "installed"])
    with pytest.raises(RuntimeError, match="isolated installed interpreter"):
        exec(compile(remotion_runtime._SCHEMA_PROBE, "<schema-probe>", "exec"), {})


@pytest.mark.parametrize("payload", [
    {"tracks": [], "clips": "invalid"},
    {"tracks": [{"id": "v", "kind": "invalid", "label": "Video"}], "clips": []},
    {"tracks": [], "clips": [{"id": "c", "at": "zero", "track": "v", "clipType": "media"}]},
    {"tracks": [], "clips": [], "output": {"fps": "thirty"}},
    {"tracks": [], "clips": [], "unknown": True},
])
def test_retained_canonical_schema_constraints(payload):
    schema = _local_schema.load_verified_schema(REPO_ROOT, str(REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        banodoco_schema.validate_timeline_config_for_container(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_retained_container_validator_refuses_nonfinite_json(value):
    payload = {"tracks": [], "clips": [], "app": {"number": value}}
    with pytest.raises(ValueError):
        banodoco_schema.validate_timeline_config_for_container(payload)


def test_role_aware_canonical_contract_is_shared_and_does_not_mutate_payload():
    payload = {"tracks": [{"id": "source", "kind": "visual", "label": "Source", "role": "source"}],
               "clips": []}
    before = copy.deepcopy(payload)
    schema = _local_schema.load_verified_schema(REPO_ROOT, str(REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH))
    jsonschema.validate(payload, schema)
    assert banodoco_schema.validate_timeline_config_for_container(payload) == before
    assert payload == before
