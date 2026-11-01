"""Host reuse must respect the caller's dependency environment."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest
from tests.lanes import unavailable

from astrid.core.execution import generic_host
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.sdk import host_bootstrap as bootstrap
from astrid.core._shared.boot_manifest import load_boot_manifest_hash


@pytest.mark.parametrize("source_changed", [False, True])
@pytest.mark.parametrize("active_children", [False, True])
@pytest.mark.parametrize("previous_python", [None, "other", "selected"])
@pytest.mark.parametrize("use_override", [False, True])
def test_bootstrap_reuses_only_the_selected_interpreter(
    tmp_path, monkeypatch, previous_python, active_children, source_changed, use_override
):
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    selected_python = "/selected-venv/bin/python"
    if use_override:
        override_python = tmp_path / "override-venv" / "bin" / "python"
        override_python.parent.mkdir(parents=True)
        override_python.write_text("#!/bin/sh\nexit 0\n")
        override_python.chmod(0o755)
        selected_python = str(override_python.absolute())
        monkeypatch.setenv(bootstrap.PACK_HOST_PYTHON_ENV, selected_python)
    else:
        monkeypatch.delenv(bootstrap.PACK_HOST_PYTHON_ENV, raising=False)
    worker = tmp_path / "support" / "credentials" / "worker"
    worker.parent.mkdir(parents=True)
    worker.write_text("test-token")
    worker.chmod(0o600)
    support = worker.parent.parent
    ready_path = support / "generic-host.ready.json"
    boot_manifest = support / "astrid-host" / "boot-manifest.json"
    compose_profile_handoff(boot_manifest, support_root=support)
    current = {
        "endpoint": "http://localhost:1234", "executor_id": bootstrap.PACK_HOST_ACTOR,
        "ready_file": str(ready_path), "credential_file": str(worker),
        "support_root": str(support), "source_checkout": str(source),
        "source_checkout_digest": "source-digest", "source_closure_digest": "closure-digest",
        "runtime_instance_id": "instance",
        "source_inventory_identity": "",
        "runtime_epoch": 1, "schema_digest": "schema", "pid": 101,
        "process_birth_id": "birth",
        "boot_manifest_path": str(boot_manifest),
        "boot_manifest_hash": load_boot_manifest_hash(boot_manifest, support_root=support),
    }
    if source_changed:
        current["source_checkout_digest"] = "previous-source-digest"
    if previous_python is not None:
        current["python_executable"] = (
            selected_python if previous_python == "selected" else "/other-venv/bin/python"
        )
    bootstrap._write_object(support / "generic-host.json", current)
    bootstrap._write_object(ready_path, {**current, "status": "ready"})
    monkeypatch.setattr(bootstrap.sys, "executable", "/selected-venv/bin/python")
    monkeypatch.setattr(generic_host, "source_checkout_digest", lambda _: "source-digest")
    monkeypatch.setattr(generic_host, "source_checkout_closure_digest", lambda _: "closure-digest")
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", lambda *args: SimpleNamespace(
        health=lambda: {"status": "ok", "runtime_epoch": 1, "schema_digest": "schema"}))
    monkeypatch.setattr(
        "astrid.core.pack.source_setup.active_source_inventory",
        lambda: SimpleNamespace(identity="", roots=(), sources=()),
    )
    monkeypatch.setattr(bootstrap, "_registered_host_status", lambda _: "ours")
    monkeypatch.setattr(bootstrap, "_IDLE_CHECK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(bootstrap, "_host_birth_identity", lambda _: "new-birth")
    monkeypatch.setattr(bootstrap, "_descendant_snapshot",
                        lambda _: {303: ("child-birth", 303)} if active_children else {})
    terminated = []
    launched = []
    # Interpreter selection is the unit boundary. Exercise the real registration/marker
    # retirement, while no process probe is started.
    monkeypatch.setattr(bootstrap, "_terminate_old_host", lambda state: terminated.append(state["pid"]))
    monkeypatch.setattr(bootstrap, "_capture_native_host_launch_identity", lambda pid: {
        "pid": pid, "uid": os.getuid(), "pgid": pid, "start_sec": 1, "start_usec": 0,
    })

    def launch(argv, **kwargs):
        launched.append(argv[0])
        boot_manifest_hash = load_boot_manifest_hash(boot_manifest, support_root=support)
        bootstrap._write_object(ready_path, {
            **current, "python_executable": argv[0], "status": "ready",
            "source_checkout_digest": "source-digest",
            "boot_manifest_hash": boot_manifest_hash,
            "pid": 202, "process_birth_id": "new-birth",
        })
        return SimpleNamespace(pid=202, poll=lambda: None)

    monkeypatch.setattr(bootstrap, "popen_owned_group", launch)
    handoff = {
        "worker_credential_file": str(worker), "source_checkout": str(source),
        "endpoint": current["endpoint"], "worker_actor": bootstrap.PACK_HOST_ACTOR,
        "worker_scopes": bootstrap.PACK_HOST_SCOPES, "runtime_instance_id": "instance",
    }
    reusable = previous_python == "selected" and not source_changed
    if active_children and not reusable:
        with pytest.raises(bootstrap.PackHostBootstrapError, match="busy with active child processes"):
            bootstrap.ensure_pack_host(handoff, reconfigure_action="reconfigure")
        assert terminated == launched == []
        assert bootstrap._read_object(support / "generic-host.json") == current
        assert bootstrap._read_object(ready_path)["pid"] == 101
        return
    result = bootstrap.ensure_pack_host(handoff, reconfigure_action="reconfigure")
    if reusable:
        assert result["host_pid"] == 101
        assert terminated == launched == []
    else:
        assert result["host_pid"] == 202
        assert terminated == [101]
        assert launched == [selected_python]
        assert bootstrap._read_object(support / "generic-host.json")["python_executable"] == launched[0]



def test_pack_host_python_override_must_be_absolute_and_executable(tmp_path, monkeypatch):
    monkeypatch.setenv(bootstrap.PACK_HOST_PYTHON_ENV, "relative/python")
    with pytest.raises(bootstrap.PackHostBootstrapError, match="absolute path"):
        bootstrap._pack_host_python_executable()

    monkeypatch.setenv(bootstrap.PACK_HOST_PYTHON_ENV, str(tmp_path / "missing-python"))
    with pytest.raises(bootstrap.PackHostBootstrapError, match="executable file") as failure:
        bootstrap._pack_host_python_executable()
    assert bootstrap.PACK_HOST_PYTHON_ENV in str(failure.value)
    assert str(tmp_path / "missing-python") in str(failure.value)

    non_executable = tmp_path / "non-executable-python"
    non_executable.write_text("not executable")
    non_executable.chmod(0o644)
    monkeypatch.setenv(bootstrap.PACK_HOST_PYTHON_ENV, str(non_executable))
    with pytest.raises(bootstrap.PackHostBootstrapError, match="executable file"):
        bootstrap._pack_host_python_executable()


@pytest.mark.integration  # needs VibeComfy installed editable in the pack-host interpreter
def test_selected_pack_host_python_uses_editable_vibecomfy_checkout(tmp_path):
    selected_python = bootstrap._pack_host_python_executable()
    probe = (
        "import importlib.metadata as metadata, json, vibecomfy; "
        "distribution = metadata.distribution('vibecomfy'); "
        "print(json.dumps({'module_file': vibecomfy.__file__, "
        "'direct_url': json.loads(distribution.read_text('direct_url.json'))}))"
    )
    child_env = {**os.environ, "PYTHONPATH": ""}
    result = subprocess.run(
        [selected_python, "-c", probe],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if os.environ.get(bootstrap.PACK_HOST_PYTHON_ENV):
            pytest.fail(
                f"configured pack-host Python cannot import VibeComfy: {result.stderr.strip()}"
            )
        unavailable("the test interpreter has no installed VibeComfy distribution")

    provenance = json.loads(result.stdout)
    module_file = Path(provenance["module_file"]).resolve()
    direct_url = provenance["direct_url"]
    installed_checkout = Path(unquote(urlparse(direct_url["url"]).path)).resolve()
    assert direct_url["dir_info"].get("editable") is True
    assert module_file == installed_checkout / "vibecomfy" / "__init__.py"
    configured_checkout = os.environ.get("ASTRID_VIBECOMFY_CHECKOUT", "").strip()
    if configured_checkout:
        assert installed_checkout == Path(configured_checkout).expanduser().resolve()


@pytest.mark.integration  # needs a VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)
def test_selected_vibecomfy_checkout_survives_generic_host_worker_boundary(
    tmp_path
):
    """Exercise the selected source through GenericPackHost and its worker."""
    configured_checkout = os.environ.get("ASTRID_VIBECOMFY_CHECKOUT", "").strip()
    if not configured_checkout:
        unavailable("ASTRID_VIBECOMFY_CHECKOUT is not configured")

    checkout = Path(configured_checkout).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    selected_python = bootstrap._pack_host_python_executable()
    fixture = (
        checkout
        / "tests"
        / "characterization"
        / "fixtures"
        / "agent_edit"
        / "case_01_widget_set"
        / "input_ui.json"
    )
    assert fixture.is_file()
    pack_root = tmp_path / "boundary-pack"
    executor_root = pack_root / "echo"
    executor_root.mkdir(parents=True)
    probe = (
        "import importlib, json; from pathlib import Path; "
        "import vibecomfy; "
        "from astrid.packs.vibecomfy.production_engine import "
        "load_workflow_path, loaded_workflow_execution_identity; "
        "m=importlib.import_module('vibecomfy.porting.import_service'); "
        "s=importlib.import_module('vibecomfy.runtime.session'); "
        "fixture=Path(%r); out=Path('{out}'); "
        "loaded=load_workflow_path(fixture, out/'child-scratch'); "
        "payload={'origin':str(Path(vibecomfy.__file__).resolve()), "
        "'revision':s.current_source_revision(), "
        "'content_digest':s.current_source_content_digest(), "
        "'apis':[callable(m.import_workflow_bytes),callable(s.current_source_revision), "
        "callable(s.current_source_content_digest), "
        "callable(s._session_composite_ownership_verified)], "
        "'identity':loaded_workflow_execution_identity(loaded)}; "
        "(out/'identity.json').write_text(json.dumps(payload,sort_keys=True),encoding='utf-8')"
        % str(fixture)
    )
    (executor_root / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "boundary.echo",
                "name": "Boundary probe",
                "kind": "external",
                "version": "1.0",
                "command": {
                    "argv": ["{python_exec}", "-c", probe],
                    # The host owns this checkout; the executor runner adds
                    # the separately validated VibeComfy root at its boundary.
                    "env": {"PYTHONPATH": str(source_root)},
                },
                "outputs": [
                    {
                        "name": "identity",
                        "type": "file",
                        "path_template": "{out}/identity.json",
                        "artifact_type": "application/json",
                    }
                ],
                "metadata": {"resource_keys": ["cpu"], "estimated_scratch_bytes": 1},
            }
        ),
        encoding="utf-8",
    )
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join(
        (str(source_root), *bootstrap._dependency_pythonpath())
    )
    attempt = tmp_path / "attempt"
    host_script = """
import json
import importlib
import sys
from pathlib import Path

from astrid.core.execution.generic_host import GenericPackHost
from astrid.packs.vibecomfy.production_engine import (
    load_workflow_path,
    loaded_workflow_execution_identity,
)
from vibecomfy.runtime.session import current_source_content_digest, current_source_revision
import vibecomfy

import_service = importlib.import_module("vibecomfy.porting.import_service")
session = importlib.import_module("vibecomfy.runtime.session")

pack_root = Path(sys.argv[1])
fixture = Path(sys.argv[2])
attempt = Path(sys.argv[3])
attempt.mkdir(parents=True, exist_ok=True)
loaded = load_workflow_path(fixture, attempt / "host-scratch")
host_payload = {
    "origin": str(Path(vibecomfy.__file__).resolve()),
    "revision": current_source_revision(),
    "content_digest": current_source_content_digest(),
    "apis": [
        callable(import_service.import_workflow_bytes),
        callable(session.current_source_revision),
        callable(session.current_source_content_digest),
        callable(session._session_composite_ownership_verified),
    ],
    "identity": loaded_workflow_execution_identity(loaded),
}
host = GenericPackHost(pack_roots=[pack_root])
host.discover()
definition, admission = host.admit("executor", "boundary.echo")
record = host.capabilities["boundary.echo"]
env, secrets = host._child_environment(record, attempt)
try:
    result = host.invoke_capability(
        capability_kind="executor",
        capability_id="boundary.echo",
        request={
            "executor_id": "boundary.echo",
            "project": "boundary-probe",
            "project_was_auto_resolved": True,
            "out": str(attempt / "outputs"),
            "run_root": str(attempt),
            "inputs": {},
            "outputs": {},
        },
        attempt=attempt,
        definition=definition,
        # The worker validates source/version while this fixture deliberately
        # avoids coupling the test to its ephemeral definition digest.
        admission={**admission, "capability_digest": ""},
        child_env=env,
    )
finally:
    env.clear()
    secrets.clear()
child_payload = json.loads(
    (attempt / "outputs" / "identity.json").read_text(encoding="utf-8")
)
print(json.dumps({"host": host_payload, "child": child_payload, "ok": result.ok}, sort_keys=True))
"""
    host_result = subprocess.run(
        [selected_python, "-c", host_script, str(pack_root), str(fixture), str(attempt)],
        cwd=source_root,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert host_result.returncode == 0, host_result.stderr
    evidence = json.loads(host_result.stdout)
    host = evidence["host"]
    executor = evidence["child"]
    assert evidence["ok"] is True
    expected_origin = str(checkout / "vibecomfy" / "__init__.py")
    assert host["origin"] == executor["origin"] == expected_origin
    assert host["revision"] == executor["revision"] == VIBECOMFY_ENGINE_REVISION
    assert host["content_digest"] == executor["content_digest"]
    assert host["content_digest"].startswith("sha256:")
    assert host["apis"] == executor["apis"] == [True, True, True, True]
    assert executor["identity"] == host["identity"]


def test_aliases_of_one_venv_interpreter_are_one_host_identity(tmp_path, monkeypatch):
    """`astrid-tools` runs bin/python3, `python -m astrid` bin/python: the same host, not a restart."""
    monkeypatch.delenv(bootstrap.PACK_HOST_PYTHON_ENV, raising=False)
    base = tmp_path / "base" / "python3.11"
    base.parent.mkdir()
    base.write_text("#!/bin/sh\n")
    base.chmod(0o755)
    names = {}
    for venv in ("venv-a", "venv-b"):
        bindir = tmp_path / venv / "bin"
        bindir.mkdir(parents=True)
        (tmp_path / venv / "pyvenv.cfg").write_text("home = base\n")
        (bindir / "python3.11").symlink_to(base)
        (bindir / "python3").symlink_to("python3.11")
        (bindir / "python").symlink_to("python3.11")
        for alias in ("python", "python3", "python3.11"):
            monkeypatch.setattr(bootstrap.sys, "executable", str(bindir / alias))
            names.setdefault(venv, set()).add(bootstrap._pack_host_python_executable())
    assert names["venv-a"] == {str(tmp_path / "venv-a" / "bin" / "python")}
    assert names["venv-b"] == {str(tmp_path / "venv-b" / "bin" / "python")}  # another venv stays distinct
