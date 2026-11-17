"""The shared installer preserves Remotion's frozen dependency contract."""

import hashlib
import json
import sys
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


def _lock_with(tmp_path, packages: dict) -> tuple[Path, Path]:
    agent = tmp_path / "source"
    remotion = agent / "remotion"
    remotion.mkdir(parents=True)
    lockfile = remotion / "package-lock.json"
    lockfile.write_text(json.dumps({"lockfileVersion": 3, "packages": {"": {}, **packages}}))
    return agent, remotion


def _fill(remotion: Path, names, *, with_manifest: bool) -> None:
    for name in names:
        folder = remotion / "node_modules" / name
        folder.mkdir(parents=True, exist_ok=True)
        if with_manifest:
            (folder / "package.json").write_text("{}")


def test_npm_exit_zero_with_empty_tree_never_publishes_marker(tmp_path, capsys):
    agent, remotion = _lock_with(tmp_path, {
        "node_modules/react": {"version": "18.0.0"},
        "node_modules/esbuild": {"version": "0.19.0"},
    })
    log = tmp_path / "install.log"
    backend = Backend(tmp_path, log, Path("uv"))

    # Fake npm: exits 0 but leaves empty package folders, as after TLS failures.
    def run(argv, *, cwd, extra_env):
        _fill(cwd, ["react", "esbuild"], with_manifest=False)

    backend.run = run
    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is False
    assert not (remotion / "node_modules/.astrid-package-lock.sha256").exists()
    out = capsys.readouterr().out
    assert "Rendering unavailable" in out and "react" in out and str(log) in out
    assert backend.outcomes == {"renderer": {"ok": False}}


def test_stale_marker_over_empty_tree_runs_npm_ci_again(tmp_path):
    agent, remotion = _lock_with(tmp_path, {"node_modules/react": {"version": "18.0.0"}})
    lock_hash = hashlib.sha256((remotion / "package-lock.json").read_bytes()).hexdigest()
    _fill(remotion, ["react"], with_manifest=False)  # an earlier run left this empty
    (remotion / "node_modules/.astrid-package-lock.sha256").write_text(lock_hash + "\n")
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    calls = []

    def run(argv, *, cwd, extra_env):
        calls.append(argv)
        if "ci" in argv:
            _fill(cwd, ["react"], with_manifest=True)

    backend.run = run
    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is True
    assert any("ci" in argv for argv in calls)
    assert (remotion / "node_modules/react/package.json").is_file()
    assert (remotion / "node_modules/.astrid-package-lock.sha256").read_text() == lock_hash + "\n"


def test_complete_tree_with_matching_marker_is_reused(tmp_path):
    agent, remotion = _lock_with(tmp_path, {
        "node_modules/react": {"version": "18.0.0"},
        "node_modules/@remotion/cli": {"version": "4.0.0"},
    })
    lock_hash = hashlib.sha256((remotion / "package-lock.json").read_bytes()).hexdigest()
    _fill(remotion, ["react", "@remotion/cli"], with_manifest=True)
    (remotion / "node_modules/.astrid-package-lock.sha256").write_text(lock_hash + "\n")
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    calls = []
    backend.run = lambda argv, *, cwd, extra_env: calls.append(argv)

    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is True
    assert calls == [["node", remotion / "scripts" / "fetch-shared-browser.mjs"]]


def test_tree_check_ignores_packages_for_other_platforms(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    other = "win32" if sys.platform != "win32" else "linux"
    _agent, remotion = _lock_with(tmp_path, {
        "node_modules/react": {"version": "18.0.0"},
        "node_modules/@esbuild/other": {"version": "0.19.0", "os": [other], "optional": True},
        "node_modules/linked": {"link": True},
    })
    _fill(remotion, ["react"], with_manifest=True)

    assert _remotion_tree_gaps(remotion) == []


def test_renderer_warning_reaches_the_success_headline():
    from astrid.installer.outcome import Outcome, receipt_warnings

    assert receipt_warnings({"renderer": {"ok": False}}) == ("renderer-incomplete",)
    assert receipt_warnings({"renderer": {"ok": True}}) == ()
    headline = Outcome("repaired", 12, warnings=receipt_warnings({"renderer": {"ok": False}})).headline()
    assert headline[0] == "Astrid is installed and running (Release 12), but frame looks and renders are unavailable."
    assert "install line again" in headline[1]


def test_missing_renderer_package_is_named_in_plain_words():
    from astrid.packs.rendering.rendering.renderers.remotion.run import _missing_renderer_package

    stderr = ("node:internal/modules/esm/resolve:1\n"
              "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '/x/remotion/node_modules/react/index.js' imported from /x/a.mjs")
    message = _missing_renderer_package(stderr)
    assert "package react is missing" in message and "install line again" in message
    assert _missing_renderer_package("Error: something else") is None
