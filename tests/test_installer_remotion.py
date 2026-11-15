"""The shared installer preserves Remotion's frozen dependency contract."""

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend


def test_remotion_dependencies_follow_lockfile_marker(tmp_path):
    agent = tmp_path / "source"
    remotion = agent / "remotion"
    remotion.mkdir(parents=True)
    lockfile = remotion / "package-lock.json"
    lockfile.write_text('{"lockfileVersion":3}\n')
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    calls = []

    def run(argv, *, cwd, extra_env):
        calls.append((argv, cwd, extra_env))
        (cwd / "node_modules").mkdir(exist_ok=True)

    backend.run = run
    env = {"ASTRID_NODE_EXECUTABLE": "/pinned/node"}
    npm = Path("/pinned/npm-cli.js")
    backend.install_remotion_dependencies(agent, env, npm)
    backend.install_remotion_dependencies(agent, env, npm)
    lockfile.write_text('{"lockfileVersion":3,"changed":true}\n')
    backend.install_remotion_dependencies(agent, env, npm)
    ci = (["/pinned/node", npm, "ci", "--no-audit", "--no-fund"], remotion, env)
    browser = (["/pinned/node", remotion / "scripts" / "fetch-shared-browser.mjs"], remotion, env)
    # The browser is fetched (or found) on every install, so renders never download it.
    assert calls == [ci, browser, browser, ci, browser]
    marker = remotion / "node_modules" / ".astrid-package-lock.sha256"
    assert marker.read_text() == hashlib.sha256(lockfile.read_bytes()).hexdigest() + "\n"


def test_remotion_failed_install_never_publishes_marker(tmp_path):
    agent = tmp_path / "source"
    remotion = agent / "remotion"
    remotion.mkdir(parents=True)
    (remotion / "package-lock.json").write_text('{}\n')
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    backend.run = Mock(side_effect=subprocess.CalledProcessError(1, "npm"))
    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")
    ) is False
    assert not (remotion / "node_modules/.astrid-package-lock.sha256").exists()


def test_update_writes_the_latest_process_control_setting(tmp_path):
    from runtime_protocol import process_control

    data = tmp_path / "data"
    support = data / "runtime"
    support.mkdir(parents=True)
    process_control.write_setting(support, "sandbox")
    backend = Backend.__new__(Backend)
    backend.process_control = "strict"
    backend.runtime = Mock()

    backend.upgrade(None, data)

    assert process_control.read_setting(support) == "strict"


@pytest.mark.node
def test_shared_browser_reinstall_skips_second_download():
    result = subprocess.run(
        ["node", "--test", "remotion/tests/shared-browser.test.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_remotion_failed_install_says_rendering_is_unavailable_and_where_the_log_is(tmp_path, capsys):
    agent = tmp_path / "source"
    remotion = agent / "remotion"
    remotion.mkdir(parents=True)
    (remotion / "package-lock.json").write_text('{}\n')
    log = tmp_path / "install.log"
    backend = Backend(tmp_path, log, Path("uv"))
    backend.run = Mock(side_effect=subprocess.CalledProcessError(1, "npm"))

    backend.install_remotion_dependencies(agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js"))

    out = capsys.readouterr().out
    assert "Rendering unavailable" in out
    assert str(log) in out
