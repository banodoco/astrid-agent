"""Declared helpers retain readiness identity through the real confined seam."""
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import wave

import pytest

from astrid.core.execution import declared_executables as binaries
from astrid.core.execution import generic_host as host
from tests.test_generic_host import _write_manifest


@pytest.fixture
def selected(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    executable = installed / "ffmpeg"
    executable.write_bytes(b"synthetic helper")
    executable.chmod(0o700)
    library = installed / "libhelper.dylib"
    library.write_bytes(b"synthetic library")
    monkeypatch.setattr(binaries, "_load_commands", lambda path: (
        ((str(library), "/usr/lib/libSystem.B.dylib"), ()) if path == executable else ((), ())))
    monkeypatch.setattr(binaries.sys, "platform", "darwin")
    return binaries.ExecutableAdmission(str(installed)), executable, library


def record_for(tmp_path, admission):
    pack = tmp_path / "pack"
    manifest = _write_manifest(pack)
    definition = json.loads(manifest.read_text())
    definition["isolation"] = {"mode": "subprocess", "network": False, "binaries": ["ffmpeg"]}
    manifest.write_text(json.dumps(definition))
    h = host.GenericPackHost(pack_roots=[pack])
    h._executable_admission = admission
    h.discover()
    return h, h.preflight("test.echo")[0]


def build(tmp_path, admission, record, monkeypatch):
    monkeypatch.setattr(host, "_verify_admitted_source", lambda _: None)
    # Contract tests inspect projection; the real test below uses Seatbelt.
    monkeypatch.setattr(host.shutil, "which", lambda name, **kw: "/usr/bin/sandbox-exec")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    authority = host._AuthenticatedAttempt((("project_id", "synthetic-project"), ("fence", 7)))
    policy = host._AttemptPolicyFactory(admission).build(authority, attempt=attempt, record=record)
    return policy, authority


@pytest.mark.skipif(sys.platform != "darwin", reason="Python 3.12 framework fixture requires macOS")
def test_python_312_framework_runtime_library_resolves_exact_file(monkeypatch):
    prefix = Path("/Library/Frameworks/Python.framework/Versions/3.12")
    expected = prefix / "Python"
    assert expected.is_file(), "the supported-host Python 3.12 framework file is unavailable"
    monkeypatch.setattr(host.sys, "base_prefix", str(prefix))
    monkeypatch.setattr(host.sysconfig, "get_config_var",
                        lambda name: "Python" if name == "PYTHONFRAMEWORK" else None)
    assert host._python_runtime_library(prefix) == expected.resolve(strict=True)


def test_non_framework_runtime_library_uses_exact_configured_shared_file(tmp_path, monkeypatch):
    prefix = tmp_path / "python"
    libdir = prefix / "lib"
    libdir.mkdir(parents=True)
    library = libdir / "libpython3.12.so"
    library.write_bytes(b"synthetic runtime library")
    config = {"PYTHONFRAMEWORK": "", "Py_ENABLE_SHARED": 1,
              "LIBDIR": str(libdir), "LDLIBRARY": library.name}
    monkeypatch.setattr(host.sysconfig, "get_config_var", lambda name: config.get(name))
    assert host._python_runtime_library(prefix) == library.resolve(strict=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Python 3.12 framework fixture requires macOS")
def test_python_312_framework_policy_grants_only_runtime_file(selected, tmp_path, monkeypatch):
    prefix = Path("/Library/Frameworks/Python.framework/Versions/3.12")
    expected = (prefix / "Python").resolve(strict=True)
    monkeypatch.setattr(host.sys, "base_prefix", str(prefix))
    monkeypatch.setattr(host.sysconfig, "get_config_var",
                        lambda name: "Python" if name == "PYTHONFRAMEWORK" else None)
    admission, _, _ = selected
    _, record = record_for(tmp_path, admission)
    policy, _ = build(tmp_path, admission, record, monkeypatch)
    profile = host._confined_launch_plan([sys.executable], policy=policy).argv[2]
    assert f'(allow file-read* (literal "{expected}"))' in profile
    for directory in (Path("/Library/Frameworks"), Path("/Library/Frameworks/Python.framework"), prefix):
        assert f'(allow file-read* (literal "{directory}"))' not in profile
        assert f'(allow file-read* (subpath "{directory}"))' not in profile
        assert f'(allow file-map-executable (literal "{directory}"))' not in profile


@pytest.mark.parametrize("state", ["absent", "replaced-outside-prefix"])
def test_configured_python_runtime_library_refuses_before_spawn(selected, tmp_path, monkeypatch, state):
    prefix = tmp_path / "Python.framework" / "Versions" / "3.12"
    prefix.mkdir(parents=True)
    configured = prefix / "Python"
    if state == "replaced-outside-prefix":
        replacement = tmp_path / "replacement" / "Python"
        replacement.parent.mkdir()
        replacement.write_bytes(b"replacement runtime")
        configured.symlink_to(replacement)
    monkeypatch.setattr(host.sys, "base_prefix", str(prefix))
    monkeypatch.setattr(host.sysconfig, "get_config_var",
                        lambda name: "Python" if name == "PYTHONFRAMEWORK" else None)
    admission, _, _ = selected
    h, record = record_for(tmp_path, admission)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    spawn_calls = []
    monkeypatch.setattr(host, "_verify_admitted_source", lambda _: None)
    monkeypatch.setattr(host.shutil, "which", lambda name, **kw: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(host, "popen_owned_group", lambda *a, **k: spawn_calls.append((a, k)))
    authority = host._AuthenticatedAttempt((("project_id", "synthetic-project"), ("fence", 7)))
    with pytest.raises(host.HostError, match="configured Python runtime library"):
        host._AttemptPolicyFactory(admission).build(authority, attempt=attempt, record=record)
    assert spawn_calls == []


def test_readiness_and_authenticated_policy_retain_exact_identity(selected, tmp_path, monkeypatch):
    admission, executable, library = selected
    _, record = record_for(tmp_path, admission)
    assert record.ready
    binding = record.executable_bindings[0]
    assert binding.executable.resolved == executable
    assert binding.libraries[0].resolved == library
    assert record.preflight["binaries"]["bindings"] == [binding.evidence()]
    policy, authority = build(tmp_path, admission, record, monkeypatch)
    assert policy.claim_binding == authority.fields
    assert policy.executable_bindings == record.executable_bindings
    plan = host._confined_launch_plan([sys.executable], policy=policy,
        parent_environment={"PATH": str(tmp_path), "DYLD_LIBRARY_PATH": str(tmp_path)})
    assert dict(plan.environment)["PATH"] == str(policy.executable_directory) + ":/usr/bin:/bin"
    assert "DYLD_LIBRARY_PATH" not in dict(plan.environment)
    profile = plan.argv[2]
    assert f'(allow file-read* (literal "{library}"))' in profile
    assert f'(allow file-read* (subpath "{executable.parent}"))' not in profile
    assert f'(allow file-write* (subpath "{policy.executable_directory}"))' not in profile
    assert '(deny default)' in profile


@pytest.mark.parametrize("changed", ["executable", "library", "unavailable", "alias"])
def test_replaced_or_unavailable_bound_inputs_refuse_before_spawn(selected, tmp_path, monkeypatch, changed):
    admission, executable, library = selected
    h, record = record_for(tmp_path, admission)
    policy, _ = build(tmp_path, admission, record, monkeypatch)
    if changed == "alias":
        policy.executable_directory.chmod(0o700)
        alias = policy.executable_directory / "ffmpeg"
        alias.unlink()
        alias.symlink_to(library)
    elif changed == "unavailable":
        executable.unlink()
    else:
        target = executable if changed == "executable" else library
        target.write_bytes(b"replacement")
    h._confinement_policy = policy
    monkeypatch.setattr(host, "popen_owned_group", lambda *a, **k: pytest.fail("spawn before validation"))
    with pytest.raises(host.HostError, match="declared executable"):
        h.invoke_capability(capability_kind="executor", capability_id="test.echo", request={}, attempt=policy.attempt_root)
    assert not (policy.private_write_root / ".astrid-capability-request.json").exists()


def test_readiness_never_silently_reselects_replacement(selected, tmp_path):
    admission, executable, _ = selected
    h, first = record_for(tmp_path, admission)
    executable.write_bytes(b"replacement")
    second = h.preflight("test.echo")[0]
    assert first.ready and not second.ready
    assert second.preflight["binaries"]["missing"] == ["ffmpeg"]
    assert "replaced" in second.preflight["binaries"]["reasons"][0]


def test_undeclared_and_missing_readiness_binding_refused(selected, tmp_path, monkeypatch):
    admission, _, _ = selected
    _, record = record_for(tmp_path, admission)
    factory = host._AttemptPolicyFactory(admission)
    with pytest.raises(host.HostError, match="authority"):
        factory.build({}, attempt=tmp_path, record=record)
    with pytest.raises(host.HostError, match="readiness binding"):
        factory.build(host._AuthenticatedAttempt(()), attempt=tmp_path,
                      record=replace(record, executable_bindings=()))
    with pytest.raises(binaries.ExecutableAdmissionError, match="unavailable"):
        admission.require(("not-declared-or-installed",))
    with pytest.raises(binaries.ExecutableAdmissionError, match="simple binary name"):
        admission.require(("../ffmpeg",))


def test_library_loader_tokens_and_transitive_closure(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"exe"); executable.chmod(0o700)
    lib = tmp_path / "lib.dylib"; lib.write_bytes(b"library")
    child = tmp_path / "child.dylib"; child.write_bytes(b"child")
    monkeypatch.setattr(binaries.sys, "platform", "darwin")
    commands = {executable: (("@rpath/lib.dylib",), ("@executable_path",)),
                lib: (("@loader_path/child.dylib",), ()), child: ((), ())}
    monkeypatch.setattr(binaries, "_load_commands", lambda p: commands[p])
    binding = binaries.ExecutableAdmission(str(tmp_path)).require(("ffmpeg",))[0]
    assert {p.resolved for p in binding.libraries} == {lib, child}
    child.unlink()
    with pytest.raises(binaries.ExecutableAdmissionError, match="unavailable"):
        binding.verify()


def test_symlink_hops_are_pinned_and_retargeting_is_refused(tmp_path):
    physical = tmp_path / "physical"; physical.mkdir()
    actual = physical / "lib.1.dylib"; actual.write_bytes(b"library")
    (physical / "lib.dylib").symlink_to(actual.name)
    (tmp_path / "opt").symlink_to(physical)
    selected = tmp_path / "opt/lib.dylib"
    pin = binaries.FilePin.measure(selected)
    assert physical / "lib.dylib" in pin.aliases
    same_bytes = physical / "other.dylib"; same_bytes.write_bytes(actual.read_bytes())
    (physical / "lib.dylib").unlink()
    (physical / "lib.dylib").symlink_to(same_bytes.name)
    with pytest.raises(binaries.ExecutableAdmissionError, match="replaced"):
        pin.verify()


def test_launch_cannot_add_helpers_or_write_the_lookup_directory(selected, tmp_path, monkeypatch):
    admission, executable, _ = selected
    _, record = record_for(tmp_path, admission)
    policy, _ = build(tmp_path, admission, record, monkeypatch)
    plan = host._confined_launch_plan([str(executable), "-version"], policy=policy)
    assert plan.argv[3] == str(executable)
    neighbour = tmp_path / "undeclared-executable"
    neighbour.write_bytes(b"undeclared"); neighbour.chmod(0o700)
    with pytest.raises(host.HostError, match="not an enumerated bootstrap"):
        host._confined_launch_plan([str(neighbour)], policy=policy)
    extra = policy.executable_directory / "undeclared"
    extra.symlink_to(executable)
    with pytest.raises(host.HostError, match="namespace differs"):
        host._confined_launch_plan([sys.executable], policy=policy)
    extra.unlink()
    with pytest.raises(host.HostError, match="executable directory"):
        host._confined_launch_plan([sys.executable], policy=replace(policy,
            executable_directory=policy.private_write_root))


def test_host_selection_survives_ambient_path_change(selected, tmp_path, monkeypatch):
    admission, executable, _ = selected
    h, record = record_for(tmp_path, admission)
    monkeypatch.setenv("PATH", str(tmp_path / "forged"))
    assert h.preflight("test.echo")[0].executable_bindings == record.executable_bindings
    executable.unlink()
    with pytest.raises(host.HostError, match="unavailable"):
        host._AttemptPolicyFactory(admission).build(host._AuthenticatedAttempt(()),
            attempt=tmp_path / "never-created", record=record)
    assert not (tmp_path / "never-created").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="actual confined helper requires macOS")
def test_actual_confined_music_level_uses_declared_ffmpeg(tmp_path, monkeypatch):
    pack = Path(host.__file__).resolve().parents[2] / "packs/music"
    h = host.GenericPackHost(pack_roots=[pack])
    h.discover()
    record = h.preflight("music.level")[0]
    assert record.ready, record.preflight
    attempt = tmp_path / "attempt"; attempt.mkdir()
    authority = host._AuthenticatedAttempt(tuple(dict(project_id="synthetic-project", fence=7,
        task_id="synthetic-task", attempt_id="synthetic-attempt", lease_id="synthetic-lease",
        runtime_epoch="synthetic-epoch", operation="music.level", executor_id="synthetic-host").items()))
    policy = host._AttemptPolicyFactory(h._executable_admission).build(authority, attempt=attempt, record=record)
    h._confinement_policy = policy
    h._attempt_claim_authority = authority
    confirmations = []
    class ClaimClient:
        def confirm_claim_authority(self, **fields):
            assert fields == dict(authority.fields)
            confirmations.append(fields)
            return authority
    h.client = ClaimClient()
    launches = []
    compile_launch = h._workload_launch_plan
    def capture_launch(*args, **kwargs):
        plan = compile_launch(*args, **kwargs)
        launches.append(plan)
        return plan
    monkeypatch.setattr(h, "_workload_launch_plan", capture_launch)
    audio = policy.project.project_root / "tone.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * i / 16000)))
                                    for i in range(16000)))
    _, admitted = h.admit("executor", "music.level")
    # Command actions take this seam in _run_claim, rather than the separate
    # native executor worker (which would add an unnecessary Python child).
    result = h._run_command_definition(record, {"audio": str(audio), "from_s": 0, "to_s": 1},
        policy.private_write_root, attempt, admission=admitted)
    assert result.returncode == 0, result
    measured = json.loads((policy.private_write_root / "level.json").read_text())
    assert measured["method"] == "ffmpeg_ebur128" and math.isfinite(measured["integrated_lufs"])
    assert (policy.executable_directory / "ffmpeg").resolve() == record.executable_bindings[0].executable.resolved
    assert not h._active_processes
    assert confirmations == [dict(authority.fields)]
    assert len(launches) == 1
    plan = launches[0]
    print("DECLARED_FFMPEG_MUSIC_LEVEL", json.dumps({"measurement": measured,
        "binding": record.executable_bindings[0].evidence(), "claim": dict(authority.fields),
        "capability_digest": record.capability_digest, "source_digest": record.source_digest,
        "policy_sha256": hashlib.sha256(plan.argv[2].encode()).hexdigest(),
        "policy": plan.argv[2], "launch_argv": plan.argv,
        "environment": dict(plan.environment), "process_id": result.process_id,
        "result_bytes": (policy.private_write_root / "level.json").read_text(),
        "result_sha256": hashlib.sha256((policy.private_write_root / "level.json").read_bytes()).hexdigest()}))
