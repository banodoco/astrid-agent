"""The installed pod command reaches the existing validated local job parser."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.packs.runpod.machine import generation_argv


@dataclass
class Installed:
    target: Path
    cwd: Path
    env: dict = field(repr=False)


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    import runtime_protocol

    root = tmp_path_factory.mktemp("installed-pod-job")
    target = root / "installed"
    repo = Path(__file__).resolve().parents[3]
    built = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation",
                            "--target", str(target), str(repo)], cwd=root,
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stdout + built.stderr
    paths = [str(target), str(Path(runtime_protocol.__file__).resolve().parent.parent),
             *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and Path(p).resolve() != repo]]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    probe = subprocess.run([sys.executable, "-c",
        "import json,astrid.console,astrid.core.gateway.dispatch,astrid.core.generation.cli;"
        "print(json.dumps([astrid.console.__file__,astrid.core.gateway.dispatch.__file__,astrid.core.generation.cli.__file__]))"],
        cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    assert all(Path(path).is_relative_to(target) for path in json.loads(probe.stdout))
    return Installed(target, root, env)


@pytest.mark.installed_smoke
@pytest.mark.parametrize("entrypoint", ["console", "module"])
@pytest.mark.parametrize("payload,mode,message", [
    ({}, "t2i", "job file is not astrid.machine-job.v1"),
    ({"schema": "astrid.machine-job.v1", "align": "check", "inputs": {}, "entry": "other-entry"},
     "t2i", "differs from the requested"),
    ({"schema": "astrid.machine-job.v1", "align": "check", "inputs": {"prompt": "cat"},
      "entry": "qwen-image-2.1-turbo", "execution": {"python": "/unused/workflow.py"}, "bindings": {}},
     "unsupported-mode", "does not support mode"),
], ids=["malformed-job", "entry-mismatch", "invalid-mode"])
def test_installed_exact_pod_argv_reaches_job_validation(installed, tmp_path, entrypoint, payload, mode, message):
    target, cwd, env = installed.target, installed.cwd, installed.env
    (tmp_path / "job.json").write_text(json.dumps(payload))
    args = generation_argv("qwen-image-2.1-turbo", str(tmp_path), mode=mode)
    executable = [sys.executable, str(target / "bin/astrid")] if entrypoint == "console" else [sys.executable, "-m", "astrid"]
    checked = subprocess.run([*executable, *args[1:]], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 2
    assert message in checked.stdout
    assert "invalid choice" not in checked.stderr
    help_result = subprocess.run([*executable, *args[1:], "--help"], cwd=cwd, env=env,
                                 capture_output=True, text=True, timeout=30)
    assert help_result.returncode == 0, help_result.stderr
    assert "--job FILE" in help_result.stdout and "entry" in help_result.stdout


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
def test_public_named_job_keeps_entry_mode_and_align(monkeypatch, tmp_path, capsys, entrypoint):
    from astrid import console
    from astrid.core import gateway
    from astrid.core.generation import local_job

    calls = []
    def run(entry, job_path, out, *, mode, align):
        calls.append((entry, job_path, out, mode, align))
        return {"outputs": []}
    monkeypatch.setattr(local_job, "run_job", run)
    args = generation_argv("qwen-image-2.1-turbo", str(tmp_path), mode="t2i")
    main = console.main if entrypoint == "console" else gateway.main
    assert main([*args[1:], "--align", "check"]) == 0
    assert calls == [("qwen-image-2.1-turbo", str(tmp_path / "job.json"), str(tmp_path / "out"), "t2i", "check")]
    assert json.loads(capsys.readouterr().out) == {"outputs": []}


def test_generate_list_stays_on_inventory_route(monkeypatch):
    from astrid import console
    from astrid.core.generation import listing

    calls = []
    monkeypatch.setattr(listing, "main", lambda argv: calls.append(argv) or 0)
    assert console.main(["generate", "list", "--json"]) == 0
    assert calls == [["list", "--json"]]
