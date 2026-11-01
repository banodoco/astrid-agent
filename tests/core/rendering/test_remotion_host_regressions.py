"""Render admission regressions across launcher, worker and platform boundaries."""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from astrid.core.rendering import remotion_runtime as runtime
from astrid.core.subprocess_env import build_child_subprocess_env
from astrid.sdk import host_bootstrap
from tests.packs.rendering.test_render_export_task_adapter import _owned_validation_fixture


def test_staged_schema_applies_native_windows_permissions(tmp_path, monkeypatch):
    from runtime_protocol import platform

    from tests.packs.rendering import test_render_export_task_adapter as adapter

    restricted = []
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "owner_only", lambda path: restricted.append(path))
    source = tmp_path / "selected-agent"
    adapter._stage_schema(source)
    assert set(restricted) == {source, *source.rglob("*")}


def test_installed_launch_proof_survives_worker_and_render_environment_filters(tmp_path, monkeypatch):
    def provisioned_registries(project, contexts, python, **_kwargs):
        # This test isolates profile propagation from installed registry discovery.
        result = {}
        for variants in contexts.values():
            for env in variants:
                context = runtime._registry_context(env, generator=False)
                context[runtime.REMOTION_PROJECT_DIR_ENV] = str(project)
                result[runtime._canonical(context)] = {"mode": "provisioned", "outputs": [], "state_file": None}
        return result

    monkeypatch.setattr(runtime, "_settle_registries", provisioned_registries)
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    project, _, _, env, reference, _ = _owned_validation_fixture(
        tmp_path, monkeypatch,
        environment_overrides={"ASTRID_HOST_EXECUTION_PROFILE": "installed"},
    )
    worker = build_child_subprocess_env(base=env, parent={**env, **reference})
    worker["ASTRID_REMOTION_VALIDATION_ROLE"] = "worker"
    render = build_child_subprocess_env(base=worker, parent=worker)
    # A separate process cannot see the launcher's ambient profile.
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if k != "ASTRID_HOST_EXECUTION_PROFILE"})
    entry = runtime._validated_runtime(project, environment=render)
    assert entry["context"]["schema_profile"] == "installed"


@pytest.mark.parametrize("failure", [
    FileNotFoundError("missing Node"),
    runtime.RemotionValidationError("partial npm installation"),
    subprocess.TimeoutExpired("node", 5),
])
def test_settled_optional_validation_failure_does_not_abort_host(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "source")
    env = {"HOME": str(tmp_path), "ASTRID_REMOTION_PROJECT_DIR": str(tmp_path / "remotion"),
           "ASTRID_REMOTION_VALIDATION_PATH": "stale-reference"}

    def refuse(**_kwargs):
        raise failure

    monkeypatch.setattr(runtime, "prepare_remotion_validation", refuse)
    assert host_bootstrap._prepare_render_validation(tmp_path, env, tmp_path, sys.executable) is None
    assert "ASTRID_REMOTION_VALIDATION_PATH" not in env
    status = runtime.remotion_runtime_status(require_explicit_project=True, environment=env)
    assert not status.available


@pytest.mark.parametrize("prior", [False, True])
def test_uncertain_validation_cleanup_still_aborts_host(tmp_path, monkeypatch, prior):
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "source")
    env = {"HOME": str(tmp_path), "ASTRID_REMOTION_PROJECT_DIR": str(tmp_path / "remotion")}
    failure = runtime.RemotionValidationError("unsettled original child")
    failure.cleanup_error = RuntimeError("cleanup unproved")
    if prior:
        retained = failure
        failure = runtime.RemotionValidationError("prior custody is unresolved")
        failure.__cause__ = retained

    def refuse(**_kwargs):
        raise failure

    monkeypatch.setattr(runtime, "prepare_remotion_validation", refuse)
    with pytest.raises(runtime.RemotionValidationError):
        host_bootstrap._prepare_render_validation(tmp_path, env, tmp_path, sys.executable)


def test_windows_registry_config_uses_userprofile_without_home(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    files = runtime._registry_config_files({"USERPROFILE": str(tmp_path), "PATH": ""})
    assert tmp_path / ".gitconfig" in files
    assert tmp_path / ".config/git/config" in files


@pytest.mark.parametrize("changed_size", [False, True])
def test_windows_content_binding_ignores_emulated_permissions_but_checks_identity(
    tmp_path, monkeypatch, changed_size,
):
    path = tmp_path / "input"
    path.write_bytes(b"exact inputs")
    original = os.fstat

    def windows_fstat(fd):
        facts = original(fd)
        values = {name: getattr(facts, name) for name in (
            "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid")}
        values["st_mode"] = facts.st_mode & 0o170000
        if changed_size:
            values["st_size"] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(os, "fstat", windows_fstat)
    if changed_size:
        with pytest.raises(runtime.RemotionValidationError, match="changed during content binding"):
            runtime._file_binding(path, digest_memo={})
    else:
        assert runtime._file_binding(path, digest_memo={})["sha256"] == runtime._file_binding(path)["sha256"]


@pytest.mark.parametrize("name", ["NODE_OPTIONS", "PYTHONUTF8", "LD_LIBRARY_PATH", "GIT_DIR"])
def test_ambient_controls_excluded_from_probes_do_not_change_launch_context(tmp_path, name):
    clean = {"HOME": str(tmp_path), "PATH": os.defpath}
    ambient = {**clean, name: "ambient-only"}
    assert runtime.probe_context(environment=ambient, python_executable=sys.executable) == runtime.probe_context(
        environment=clean, python_executable=sys.executable)
    assert runtime._registry_context(ambient, generator=False) == runtime._registry_context(clean, generator=False)


@pytest.mark.parametrize("declaration", ["env_passthrough", "command_env", "required_env"])
@pytest.mark.parametrize("name", ["NODE_OPTIONS", "PYTHONUTF8", "LD_LIBRARY_PATH", "GIT_DIR"])
def test_declared_render_worker_controls_are_refused_before_launch(tmp_path, monkeypatch, name, declaration):
    import json

    from astrid.core.execution.generic_host import (
        GenericPackHost,
        HostError,
        _launch_worker_probe_environments,
    )
    from tests.test_generic_host import _write_manifest

    manifest = _write_manifest(tmp_path / "render-worker", capability_id="test.render")
    data = json.loads(manifest.read_text())
    data["metadata"]["requires_remotion"] = True
    environment = {"HOME": str(tmp_path), "PATH": os.defpath, name: "controlled"}
    if declaration == "env_passthrough":
        data["isolation"] = {"mode": "subprocess", "env_passthrough": [name]}
    elif declaration == "command_env":
        data["command"]["env"] = {name: "controlled"}
    else:
        data["metadata"]["required_env"] = [name]
    manifest.write_text(json.dumps(data))
    with pytest.raises(runtime.RemotionValidationError, match=name):
        _launch_worker_probe_environments((tmp_path,), environment, remotion_only=True)
    monkeypatch.setenv(name, "controlled")
    host = GenericPackHost(pack_roots=[tmp_path], credential_source=environment)
    record = host.discover()[0]
    with pytest.raises(HostError, match=name):
        host._child_environment(record, tmp_path / "attempt", explicit_env=dict(record.definition.command.env))


def test_execution_profile_cannot_be_overridden_by_command_environment():
    from astrid.core.subprocess_env import SubprocessEnvPolicyError

    with pytest.raises(SubprocessEnvPolicyError, match="parent-owned"):
        build_child_subprocess_env(parent={"ASTRID_HOST_EXECUTION_PROFILE": "installed"},
                                   explicit_env={"ASTRID_HOST_EXECUTION_PROFILE": "source"})


def test_launch_bound_registry_consumption_uses_validated_projected_state(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run

    expected = {"hash": "validated", "content_hashes": {"effects": "exact"}}
    monkeypatch.setenv("ASTRID_REMOTION_VALIDATION_LAUNCH", "a" * 32)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "ambient-only-gitconfig"))
    monkeypatch.setattr(runtime, "_validated_runtime", lambda _project: {
        "registry": {"mode": "source_settled", "expected_state": expected}})
    monkeypatch.setattr(run, "_read_registry_state", lambda _project: expected)
    monkeypatch.setattr(run, "_registry_output_content_hashes", lambda _project: expected["content_hashes"])

    def ambient_reinspection(_theme):
        pytest.fail("validated registry corpus must not be recomputed under ambient Git controls")

    monkeypatch.setattr(run, "_effective_registry_state", ambient_reinspection)
    run._regenerate_element_registries_locked(tmp_path, None)


@pytest.mark.parametrize("outcome", ["failed", "timeout", "changed_version", "settled_refusal", "unsettled"])
def test_admission_reprobes_node_after_successful_launch(tmp_path, monkeypatch, outcome):
    project, _, node, env, reference, _ = _owned_validation_fixture(tmp_path, monkeypatch)
    # The content-bound executable and all launch inputs are still unchanged;
    # its external runtime dependencies can fail after the successful launch.
    runtime._validated_runtime(project, environment={**env, **reference})
    calls = []

    def probe(argv, *, cwd, env, timeout):
        calls.append((argv, cwd, dict(env), timeout))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if outcome == "unsettled":
            raise runtime.RemotionProbeCleanupError("cleanup unproved")
        if outcome == "settled_refusal":
            raise RuntimeError("probe admission refused and child settled")
        return subprocess.CompletedProcess(argv, 1 if outcome == "failed" else 0,
                                           "v22.0.0\n", "Node dependency unavailable"), {}

    monkeypatch.setattr(runtime, "_owned_probe", probe)
    if outcome == "unsettled":
        with pytest.raises(runtime.RemotionProbeCleanupError):
            runtime.resolve_remotion_runtime_tools(project, environment={**env, **reference})
    else:
        tools, reason = runtime.resolve_remotion_runtime_tools(project, environment={**env, **reference})
        assert tools is None and reason
    assert len(calls) == 1
    assert calls[0] == ([str(node), "--version"], project, runtime._probe_environment(env), 5)


def test_captured_orchestrator_uses_platform_descriptor_launch(tmp_path, monkeypatch):
    from runtime_protocol import platform

    from astrid.core.execution import generic_host
    from astrid.core.execution.orchestrator import runner
    from astrid.core.execution.orchestrator.schema import validate_orchestrator_definition

    definition = validate_orchestrator_definition({
        "schema_version": 1, "id": "demo.run", "name": "Demo", "kind": "external", "version": "1.0",
        "runtime": {"kind": "python", "module": "external_pkg.run", "function": "run"},
    })
    selected = {"definition": {"capability_kind": "orchestrator", "capability_id": definition.id,
                               "definition_digest": generic_host._capability_digest(definition.to_dict())},
                "source": {"root": str(tmp_path / "external_pkg"), "contents": {"run.py": "captured"}}}
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    launches = []

    class Child:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self):
            return "", ""

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        descriptor, = kwargs["pass_fds"]
        assert os.fstat(descriptor).st_size > 0
        (tmp_path / ".astrid-python-orchestrator.result").write_bytes(pickle.dumps({"outputs": {"answer": "captured"}}))
        return Child()

    monkeypatch.setattr(platform, "spawn_tree", launch)
    original = subprocess.run

    def no_raw_descriptors(argv, **kwargs):
        assert not kwargs.get("pass_fds"), "raw subprocess.run cannot inherit descriptors on Windows"
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_raw_descriptors)
    result = runner._run_python_orchestrator_subprocess(
        definition, runner.OrchestratorRunRequest(orchestrator_id=definition.id, run_root=tmp_path),
        _captured_source=(selected, {}))
    assert result.outputs["answer"] == "captured"
    assert len(launches) == 1
    assert launches[0][0][1:3] == ["-X", "utf8"]


def test_native_content_binding_checks_open_descriptor(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"captured source bytes")
    assert runtime._file_binding(path, digest_memo={}) == runtime._file_binding(path)


def test_native_platform_launch_inherits_source_descriptor():
    from runtime_protocol import platform

    with tempfile.TemporaryFile() as source:
        source.write(b"captured source bytes")
        source.seek(0)
        argv = [sys.executable, "-X", "utf8", "-c",
                "import os,sys; print(os.read(int(sys.argv[1]), 100).decode())", str(source.fileno())]
        with platform.spawn_tree(argv, pass_fds=(source.fileno(),), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) as child:
            try:
                stdout, stderr = child.communicate(timeout=10)
            finally:
                if child.poll() is None:
                    platform.stop_tree(child, grace=0.2)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "captured source bytes"
