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
from astrid.core.rendering import _local_schema as render_schema, remotion_runtime
from astrid.core.timeline import _local_schema
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
    monkeypatch.setattr(_local_schema.platform, "is_current_user", lambda _facts: False)
    with pytest.raises(ValueError, match="owner/mode"):
        _local_schema.verified_schema_path(root, str(schema))



@pytest.mark.parametrize("change", ["none", "origin", "digest", "owner", "regular_file", "verifier_mode"])
def test_timeline_reader_applies_verified_admission(staged_schema, tmp_path, monkeypatch, change):
    root, schema = staged_schema
    reader_path = root / "astrid/core/timeline/banodoco_schema.py"
    spec = importlib.util.spec_from_file_location("selected_timeline_reader", reader_path)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    configured = schema
    if change == "origin":
        configured = tmp_path / schema.name
        configured.write_bytes(schema.read_bytes())
    elif change == "digest":
        schema.write_text("{}")
    elif change == "owner":
        monkeypatch.setattr(_local_schema.platform, "is_current_user", lambda _facts: False)
    elif change == "regular_file":
        schema.unlink()
        schema.mkdir()
    elif change == "verifier_mode":
        (root / "astrid/core/timeline/_local_schema.py").chmod(0o777)
    monkeypatch.setenv(reader.ASTRID_TIMELINE_SCHEMA_PYTHONPATH, str(configured))
    if change == "none":
        assert reader.load_vendored_timeline_schema() == json.loads(schema.read_text())
    else:
        with pytest.raises(ValueError):
            reader.load_vendored_timeline_schema()


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
        render_schema.load_verified_schema(REPO_ROOT, str(schema))


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
        render_schema.load_verified_schema(root, str(schema_path))


def test_strict_probe_consumes_the_selected_canonical_reader(monkeypatch, capsys):
    schema = REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH
    monkeypatch.setattr(sys, "argv", ["probe", str(schema), str(REPO_ROOT), "source"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(compile(remotion_runtime._schema_probe_source(), "<schema-probe>", "exec"), {})
    closure = json.loads(capsys.readouterr().out)
    assert closure["origin"] == str(schema)
    assert str(Path(_local_schema.__file__)) in closure["module_files"]
    assert str(Path(render_schema.__file__)) in closure["module_files"]
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
    schema = render_schema.load_verified_schema(REPO_ROOT, str(REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH))
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
    schema = render_schema.load_verified_schema(REPO_ROOT, str(REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH))
    jsonschema.validate(payload, schema)
    assert banodoco_schema.validate_timeline_config_for_container(payload) == before
    assert payload == before


def _windows_acl_fixture(monkeypatch, *, owner=500, process_owner=500, trustees=(500, 18, 544),
                         flags=(16, 16, 0), ace_type=0, null_dacl=False, invalid_acl=False):
    """Model the retained SDDL with native ACE buffers and Runtime's API seam."""
    import ctypes
    from types import SimpleNamespace

    from runtime_protocol import windows_security as security

    def sid(number):
        # The capture's account SID uses domain subauthorities and RID 500.
        parts = [21, 3077485881, 3661871218, 4154919024, number] if number in (500, 501) else (
            [32, 544] if number == 544 else [number])
        return bytes([1, len(parts)]) + (5).to_bytes(6, "big") + b"".join(
            part.to_bytes(4, "little") for part in parts)

    user = ctypes.create_string_buffer(sid(500))
    token_owner = ctypes.create_string_buffer(sid(process_owner))
    file_owner = ctypes.create_string_buffer(sid(owner))
    acl = security._Acl()
    acl.count = len(trustees)
    aces = []
    for trustee, flag in zip(trustees, flags, strict=True):
        raw_sid = sid(trustee)
        length = 8 + len(raw_sid)
        aces.append(ctypes.create_string_buffer(bytes([ace_type, flag]) + length.to_bytes(2, "little")
                                               + (0x1F01FF).to_bytes(4, "little") + raw_sid))
    descriptor = ctypes.create_string_buffer(1)

    def set_pointer(out, address):
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(address)

    def get_security(_handle, _kind, _flags, owner_out, _group, dacl_out, _sacl, descriptor_out):
        set_pointer(owner_out, ctypes.addressof(file_owner))
        set_pointer(dacl_out, 0 if null_dacl else ctypes.addressof(acl))
        set_pointer(descriptor_out, ctypes.addressof(descriptor))
        return 0

    def get_ace(_dacl, index, out):
        set_pointer(out, ctypes.addressof(aces[index]))
        return 1

    def sid_bytes(value):
        header = ctypes.string_at(value, 8)
        return ctypes.string_at(value, 8 + 4 * header[1])

    def sid_text(value):
        raw = sid_bytes(value)
        return "S-1-5-" + "-".join(str(int.from_bytes(raw[i:i+4], "little"))
                                    for i in range(8, len(raw), 4))

    adv = SimpleNamespace(GetSecurityInfo=get_security, GetAce=get_ace,
                          IsValidSid=lambda _sid: True, IsValidAcl=lambda _acl: not invalid_acl,
                          GetLengthSid=lambda value: len(sid_bytes(value)),
                          EqualSid=lambda a, b: sid_bytes(a) == sid_bytes(b),
                          GetSecurityDescriptorControl=lambda _d, control, _r: 1)
    kernel = SimpleNamespace(LocalFree=lambda _descriptor: None)
    monkeypatch.setattr(security, "_api", lambda: (adv, kernel))
    monkeypatch.setattr(security, "_user_sid", lambda: user)
    monkeypatch.setattr(security, "_default_owner_sid", lambda: token_owner)
    monkeypatch.setattr(security, "_sid_text", sid_text)
    return security


def test_windows_captured_acl_same_owner_is_admitted(monkeypatch):
    # Source O:LAD:(A;ID;FA;;;LA)(A;ID;FA;;;SY)
    # Destination O:LAD:(A;;FA;;;BA)(A;ID;FA;;;SY)(A;ID;FA;;;LA)
    _windows_acl_fixture(monkeypatch, trustees=(500, 18), flags=(16, 16))
    assert _local_schema._windows_schema_handle_is_safe(1)
    _windows_acl_fixture(monkeypatch, trustees=(544, 18, 500), flags=(0, 16, 16))
    assert _local_schema._windows_schema_handle_is_safe(1)


@pytest.mark.parametrize("changes", [
    {"trustees": (500, 18, 501)},  # Foreign user with full control.
    {"trustees": (500, 18, 545)},  # BUILTIN Users.
    {"trustees": (500, 18, 11)},  # Authenticated Users.
    {"owner": 501},
    {"owner": 501, "process_owner": 544},
    {"ace_type": 1},
    {"null_dacl": True},
    {"invalid_acl": True},
    {"trustees": (544, 18, 18)},  # No effective user ACE.
])
def test_windows_schema_rejects_unsafe_acl(monkeypatch, changes):
    _windows_acl_fixture(monkeypatch, **changes)
    assert not _local_schema._windows_schema_handle_is_safe(1)


@pytest.mark.parametrize("bad", [None, "source", "destination"])
def test_windows_installed_schema_checks_both_source_and_destination(tmp_path, monkeypatch, bad):
    from types import SimpleNamespace

    version = tmp_path / "version"
    prefix = version / "py"
    root = prefix / "Lib" / "site-packages"
    for relative in (_local_schema.SCHEMA_RELATIVE_PATH, Path("runtime.lock.json"),
                     *_local_schema.READER_INPUTS):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / relative).read_bytes())
    source = version / "a" / _local_schema.SCHEMA_RELATIVE_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes((REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH).read_bytes())
    destination = root / _local_schema.SCHEMA_RELATIVE_PATH
    checked = []

    def is_safe(path):
        checked.append(path)
        return path != {"source": source, "destination": destination}.get(bad)

    monkeypatch.setattr(_local_schema, "sys", SimpleNamespace(platform="win32", prefix=str(prefix)))
    monkeypatch.setattr(_local_schema, "_windows_schema_path_is_safe", is_safe)
    if bad:
        with pytest.raises(ValueError, match="owner/mode"):
            _local_schema.verified_schema_path(root, str(destination))
    else:
        assert _local_schema.verified_schema_path(root, str(destination)) == destination
        assert source in checked and destination in checked


@pytest.mark.parametrize("layout", ["source", "installed"])
@pytest.mark.parametrize("case", ["safe", "user_distinct_default", "default_owner",
                                  "foreign", "owner", "process_owner"])
def test_windows_schema_admission_uses_sid_policy(tmp_path, monkeypatch, layout, case):
    from contextlib import contextmanager
    from types import SimpleNamespace

    version = tmp_path / "version"
    prefix = version / "py"
    root = prefix / "Lib" / "site-packages" if layout == "installed" else tmp_path / "agent"
    for relative in (_local_schema.SCHEMA_RELATIVE_PATH, Path("runtime.lock.json"),
                     *_local_schema.READER_INPUTS):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / relative).read_bytes())
    source = version / "a" / _local_schema.SCHEMA_RELATIVE_PATH
    if layout == "installed":
        source.parent.mkdir(parents=True)
        source.write_bytes((REPO_ROOT / _local_schema.SCHEMA_RELATIVE_PATH).read_bytes())
    schema = root / _local_schema.SCHEMA_RELATIVE_PATH
    security = _windows_acl_fixture(monkeypatch)
    checked = []

    @contextmanager
    def handle(path):
        checked.append(path)
        # The original source descriptor has user + SYSTEM, while the copied
        # schema has the additional explicit Administrators ACE.
        changes = {"trustees": (500, 18), "flags": (16, 16)} if path == source else {
            "trustees": (544, 18, 500), "flags": (0, 16, 16)}
        if path == schema:
            if case == "foreign":
                changes = {"trustees": (500, 18, 501)}
            elif case == "owner":
                changes["owner"] = 501
            elif case == "process_owner":
                changes["owner"] = 501
                changes["process_owner"] = 544
            elif case == "user_distinct_default":
                changes["process_owner"] = 544
            elif case == "default_owner":
                changes.update(owner=544, process_owner=544)
        _windows_acl_fixture(monkeypatch, **changes)
        yield 1, None

    monkeypatch.setattr(security, "_handle", handle)
    monkeypatch.setattr(_local_schema, "sys", SimpleNamespace(platform="win32", prefix=str(prefix)))
    monkeypatch.setattr(_local_schema.platform, "is_current_user",
                        lambda *_args: pytest.fail("POSIX ownership predicate was called"))
    if case in {"safe", "user_distinct_default", "default_owner"}:
        assert _local_schema.verified_schema_path(root, str(schema)) == schema
        assert schema in checked
        if layout == "installed":
            assert source in checked
    else:
        with pytest.raises(ValueError, match="owner/mode"):
            _local_schema.verified_schema_path(root, str(schema))


@pytest.mark.windows_native
def test_windows_native_schema_dacl_allows_privileged_and_rejects_foreign(tmp_path):
    from runtime_protocol import windows_security as security

    user = security._user_sid()
    user_sid = security._sid_text(user)
    path = tmp_path / "schema.json"
    path.write_text("{}")
    with security._handle(path, write_dac=True) as (handle, _info):
        security._set_dacl(handle, f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BA)")
    assert _local_schema._windows_schema_path_is_safe(path)
    with security._handle(path, write_dac=True) as (handle, _info):
        security._set_dacl(handle, f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BU)")
    assert not _local_schema._windows_schema_path_is_safe(path)
