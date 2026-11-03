from __future__ import annotations

import json
import os
import sys

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from tests.test_generic_host import _write_manifest


@pytest.mark.linux_native
@pytest.mark.non_root
def test_linux_nonroot_real_process_launch_and_secret_custody(tmp_path, monkeypatch):
    """The product user starts and reaps a real child; launch mappings are cleared."""
    assert sys.platform == "linux"
    assert os.geteuid() != 0, "run as the non-root product user"
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text())
    manifest["command"]["argv"][2] = (
        "import os; from pathlib import Path; "
        "Path('{out}/answer.txt').write_text(str(os.geteuid()))"
    )
    manifest_path.write_text(json.dumps(manifest))
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    original = host._child_environment
    captured = {}

    def capture_environment(*args, **kwargs):
        env, secrets = original(*args, **kwargs)
        env["PROVIDER_TOKEN"] = secrets["PROVIDER_TOKEN"] = "launch-only-secret"
        captured.update(env=env, secrets=secrets)
        return env, secrets

    monkeypatch.setattr(host, "_child_environment", capture_environment)
    try:
        result = host._run_command_definition(record, {}, output, attempt)
        answer = output / "answer.txt"
        assert result.returncode == 0
        assert int(answer.read_text()) == os.geteuid() == answer.stat().st_uid
        assert captured["env"] == {} and captured["secrets"] == {}
        assert not host._active_processes and not host._unresolved_task_scopes
    finally:
        host.shutdown()

    # Exercise the same launch finalizer when the kernel rejects exec: no fake
    # process or procfs is involved, and the retained mappings must clear.
    manifest["command"]["argv"] = ["/astrid-nonexistent-test-executable"]
    manifest_path.write_text(json.dumps(manifest))
    failed_host = GenericPackHost(pack_roots=[tmp_path])
    failed_record = failed_host.discover()[0]
    original = failed_host._child_environment
    captured.clear()
    monkeypatch.setattr(failed_host, "_child_environment", capture_environment)
    try:
        with pytest.raises(FileNotFoundError):
            failed_host._run_command_definition(failed_record, {}, output, attempt)
        assert captured["env"] == {} and captured["secrets"] == {}
        assert not failed_host._active_processes
    finally:
        failed_host.shutdown()
