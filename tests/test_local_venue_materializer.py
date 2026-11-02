"""Focused tests for the disposable local Pinokio venue."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from scripts import ci_journey_r24_materializer as materializer

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts/local_venue/run-loopback-materializer.py"
)
SPEC = importlib.util.spec_from_file_location("local_venue_materializer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
local_venue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_venue)
local_venue.MaterializerError = materializer.MaterializerError


def _provision_roots(tmp_path: Path) -> dict[str, Path]:
    pinokiod = tmp_path / "pinokiod"
    (pinokiod / "script").mkdir(parents=True)
    (pinokiod / "kernel/bin").mkdir(parents=True)
    (pinokiod / "script/install-mode.js").write_text("stock setup\n")
    (pinokiod / "kernel/bin/conda.js").write_text("stock conda\n")
    run_root = tmp_path / "run"
    home = run_root / "pinokio-home"
    runner_home = run_root / "installer-home"
    (run_root / "evidence/materializer").mkdir(parents=True)
    home.mkdir()
    runner_home.mkdir()
    node = tmp_path / "node"
    node.write_text("node\n")
    node.chmod(0o700)
    return {
        "pinokiod": pinokiod,
        "run_root": run_root,
        "home": home,
        "runner_home": runner_home,
        "node": node,
    }


def test_official_managed_home_setup_is_exact_and_verified(tmp_path, monkeypatch):
    roots = _provision_roots(tmp_path)
    calls = []

    def completed(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["env"]["PINOKIO_HOME"] == str(roots["home"])
        assert kwargs["env"]["HOME"] == str(roots["runner_home"])
        assert kwargs["env"]["PINOKIO_SETUP_MODE"] == "managed-conda"
        kwargs["receipt"].setdefault("commands", []).append({
            "label": kwargs["label"],
            "argv": argv,
            "exit": 0,
            "elapsed_seconds": 0.001,
            "log_sha256": "a" * 64,
        })
        if kwargs["label"] == "pinokio-managed-home-setup":
            conda = roots["home"] / "bin/miniforge/bin/conda"
            hook = roots["home"] / "bin/miniforge/etc/profile.d/conda.sh"
            metadata = roots["home"] / "bin/miniforge/conda-meta"
            conda.parent.mkdir(parents=True)
            hook.parent.mkdir(parents=True)
            metadata.mkdir(parents=True)
            conda.write_text("managed conda\n")
            conda.chmod(0o700)
            hook.write_text("managed hook\n")

    monkeypatch.setattr(local_venue, "_run_bounded", completed)
    local_venue._provision_managed_home(
        **roots,
        env={
            "PINOKIO_HOME": str(roots["home"]),
            "HOME": str(roots["runner_home"]),
        },
    )
    assert calls[0][0] == [
        str(roots["node"]),
        str((SCRIPT.parent / "pinokiod-install-mode.cjs").resolve()),
        str(roots["pinokiod"]),
    ]
    assert calls[1][0][-1] == "--version"
    assert calls[2][0][-2:] == ["shell.bash", "hook"]
    receipt = json.loads((
        roots["run_root"]
        / "evidence/materializer/managed-home-provision.json"
    ).read_text())
    assert receipt["status"] == "complete"
    assert receipt["setup_mode"] == "managed-conda"
    assert receipt["setup_requirements"] == [{"name": "conda"}]
    assert receipt["environment_boundary"]["home_inside_pinokio_home"] is False
    assert [row["exit"] for row in receipt["commands"]] == [0, 0, 0]


def test_local_setup_driver_only_corrects_pinned_initialization_order():
    raw = (SCRIPT.parent / "pinokiod-install-mode.cjs").read_text()
    assert raw.index("await kernel.bin.init()") < raw.index("await kernel.shell.init()")
    assert "kernel.bin.check" in raw
    assert 'requirements: [{ name: "conda" }]' in raw
    assert "kernel.bin.install({ params: checked.requirements }" in raw
    assert "kernel.bin.mod.conda.check()" in raw
    assert 'mode !== "managed-conda"' in raw


def test_local_venue_propagates_uv_system_certificates():
    ambient = {
        "HOME": "/task/home",
        "SSL_CERT_FILE": "/ambient/ca.pem",
        "UV_SYSTEM_CERTS": "false",
    }
    selected = local_venue._local_venue_environment(ambient)
    assert selected["UV_SYSTEM_CERTS"] == "true"
    assert selected["SSL_CERT_FILE"] == "/ambient/ca.pem"
    assert ambient["UV_SYSTEM_CERTS"] == "false"


def test_local_venue_routes_github_archives_outside_blocking_proxy():
    ambient = {
        "NO_PROXY": "localhost",
        "no_proxy": "127.0.0.1",
        "HTTPS_PROXY": "[redacted]",
    }
    selected = local_venue._local_venue_environment(ambient)
    assert selected["NO_PROXY"] == "localhost,github.com,codeload.github.com"
    assert selected["no_proxy"] == "127.0.0.1,github.com,codeload.github.com"
    assert selected["HTTPS_PROXY"] == "[redacted]"
    assert ambient["NO_PROXY"] == "localhost"


def test_materialized_install_action_gets_explicit_trust_overlay(tmp_path):
    resource = tmp_path / "api" / "astrid"
    action = resource / "pinokio" / "install.js"
    action.parent.mkdir(parents=True)
    action.write_text(
        "module.exports = async () => ({ run: ["
        "app.shellStep(app.bootstrapScript('install'))"
        "] });\n",
        encoding="utf-8",
    )
    selected = local_venue._patch_materialized_install_action(resource)
    assert selected == action
    assert "UV_SYSTEM_CERTS: 'true'" in action.read_text(encoding="utf-8")


def test_managed_home_setup_refuses_a_zero_exit_without_conda(tmp_path, monkeypatch):
    roots = _provision_roots(tmp_path)
    monkeypatch.setattr(local_venue, "_run_bounded", lambda *args, **kwargs: None)
    with pytest.raises(materializer.MaterializerError, match="runnable managed Miniforge"):
        local_venue._provision_managed_home(
            **roots,
            env={
                "PINOKIO_HOME": str(roots["home"]),
                "HOME": str(roots["runner_home"]),
            },
        )
    receipt = json.loads((
        roots["run_root"]
        / "evidence/materializer/managed-home-provision.json"
    ).read_text())
    assert receipt["status"] == "blocked"
    assert receipt["first_stop"] == "official-setup"


def test_known_install_failure_stops_and_records_child_exit(tmp_path):
    receipt = {"commands": []}
    started = time.monotonic()
    with pytest.raises(materializer.MaterializerError, match="child exit"):
        local_venue._run_bounded(
            [
                sys.executable,
                "-c",
                (
                    "import time; "
                    "print('ASTRID_PINOKIO_FAILURE', flush=True); "
                    "time.sleep(30)"
                ),
            ],
            log=tmp_path / "install.log",
            receipt=receipt,
            label="pinokio-install",
            env=dict(os.environ),
            cwd=tmp_path,
            timeout=60,
            disk_root=tmp_path,
            failure_marker=local_venue.FAILURE_MARKER,
        )
    assert time.monotonic() - started < 5
    command = receipt["commands"][0]
    assert command["known_failure_marker_observed"] is True
    assert command["termination_reason"] == "known-failure-marker"
    assert isinstance(command["exit"], int)
    assert command["status"] == "exited"
