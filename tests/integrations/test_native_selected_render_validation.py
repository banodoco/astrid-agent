"""A selected native action consumes the launch's real render validation."""

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from astrid.core.execution.generic_host import GenericPackHost, _launch_worker_probe_environments
from astrid.core.rendering.remotion_runtime import _registry_context, probe_context
from astrid.core.subprocess_env import build_child_subprocess_env
from astrid.sdk.host_bootstrap import _prepare_render_validation
from tests.helpers.remotion_runtime import remotion_install_problems
from tests.packs.rendering._helpers import _execution_env


@pytest.mark.integration
def test_selected_native_action_consumes_real_launch_validation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    pack = tmp_path / "selected_collection" / "nativeproof"
    action = pack / "actions" / "probe.py"
    action.parent.mkdir(parents=True)
    action.write_text(
        "import os, sys\n"
        "def run():\n"
        "    names = ('HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'LOGNAME', 'PATH', 'PWD', 'SHELL',\n"
        "             'TMPDIR', 'USER', 'USERNAME', 'VIRTUAL_ENV', 'PYENV_VERSION',\n"
        "             'ASTRID_HOST_EXECUTION_PROFILE', 'ASTRID_PACKS_PATH', 'ASTRID_SOURCE_STATE',\n"
        "             'ASTRID_TIMELINE_SCHEMA_PYTHONPATH', 'ASTRID_NODE_EXECUTABLE',\n"
        "             'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'PYTHONPATH')\n"
        "    return {'pid': os.getpid(), 'environment': {k: os.environ[k] for k in names if k in os.environ}}\n"
    )
    (pack / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "pack_api": "2.0", "id": "nativeproof",
        "name": "Native proof", "version": "1.0.0", "actions": {
            "probe": {"description": "Validate the native worker", "inputs": [], "outputs": {},
                      "invocation": {"kind": "python", "path": "actions/probe.py", "function": "run"},
                      "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"],
                                   "project_scope": "optional", "requires_remotion": True}},
        },
    }))
    problems = remotion_install_problems()
    assert not problems, "; ".join(problems)
    node = shutil.which("node")
    assert node
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "source")
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(tmp_path / "ambient"))
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", str(Path(node).resolve()))
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str((root / "remotion").resolve()))
    monkeypatch.setenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", str(root / "astrid/core/timeline/schema/timeline-config.schema.json"))

    with _execution_env():
        environment = build_child_subprocess_env()
        host = GenericPackHost(pack_roots=[pack], credential_source={})
        host.discover()
        definition, admission = host.admit("executor", "nativeproof.probe")
        projections = _launch_worker_probe_environments((pack,), environment, remotion_only=True, dispatch_only=True)
        assert _prepare_render_validation(root, environment, tmp_path / "support", sys.executable, (pack,))
        for name in ("ASTRID_REMOTION_VALIDATION_PATH", "ASTRID_REMOTION_VALIDATION_SHA256",
                     "ASTRID_REMOTION_VALIDATION_LAUNCH", "ASTRID_REMOTION_VALIDATION_ROLE"):
            monkeypatch.setenv(name, environment[name])
        attempt = tmp_path / "attempt"
        record = host.capabilities["nativeproof.probe"]
        child_env, secrets = host._child_environment(record, attempt, admission=admission)
        try:
            result = host.invoke_capability(
                capability_kind="executor", capability_id=record.id,
                request={"inputs": {}, "out": str(attempt / "outputs"), "project": "demo",
                         "project_was_auto_resolved": True, "python_exec": sys.executable,
                         "invocation": "runtime"},
                attempt=attempt, definition=definition, admission=admission, child_env=child_env,
            )
        finally:
            secrets.clear()
        facts = result.payload["action_result"]
        assert facts["pid"] != os.getpid()
        observed = facts["environment"]
        assert observed["ASTRID_PACKS_PATH"] == str(pack.parent)
        assert _registry_context(observed, generator=False) in [
            _registry_context(item, generator=False) for item in projections]
        assert probe_context(environment=observed, python_executable=sys.executable) in [
            probe_context(environment=item, python_executable=sys.executable) for item in projections]
