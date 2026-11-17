"""The shared installer preserves Remotion's frozen dependency contract."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
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


def _lock(tmp_path, packages: dict) -> tuple[Path, Path]:
    agent = tmp_path / "source"
    remotion = agent / "remotion"
    remotion.mkdir(parents=True)
    (remotion / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {"": {}, **packages}}))
    return agent, remotion


def _package(remotion: Path, key: str, meta: dict | None = None, *, version: str = "1.0.0",
             main: str | None = None, files: bool = True) -> None:
    """Write an installed package the way npm would: package.json plus its entry file."""
    folder = remotion / key
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"name": key.split("node_modules/")[-1], "version": (meta or {}).get("version", version)}
    if main is not None:
        manifest["main"] = main
    (folder / "package.json").write_text(json.dumps(manifest))
    if files:
        (folder / "index.js").write_text("module.exports = {};\n")


def _complete(remotion: Path, keys) -> None:
    """Write each named lock package at its locked version."""
    packages = json.loads((remotion / "package-lock.json").read_text())["packages"]
    for key in keys:
        _package(remotion, key, packages[key])


def test_empty_tree_after_npm_ci_never_publishes_marker(tmp_path, capsys):
    agent, remotion = _lock(tmp_path, {
        "node_modules/react": {"version": "18.0.0"},
        "node_modules/esbuild": {"version": "0.19.0"},
    })
    log = tmp_path / "install.log"
    backend = Backend(tmp_path, log, Path("uv"))

    def run(argv, *, cwd, extra_env):  # npm exits 0 but leaves empty package folders
        for name in ("react", "esbuild"):
            (cwd / "node_modules" / name).mkdir(parents=True, exist_ok=True)

    backend.run = run
    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is False
    assert not (remotion / "node_modules/.astrid-package-lock.sha256").exists()
    out = capsys.readouterr().out
    assert "Rendering unavailable" in out and "react" in out and str(log) in out
    assert backend.outcomes["renderer"] == {"ok": False, "reason": "tree"}


def test_stale_marker_over_incomplete_tree_runs_npm_ci_again(tmp_path):
    agent, remotion = _lock(tmp_path, {"node_modules/react": {"version": "18.0.0"}})
    lock_hash = hashlib.sha256((remotion / "package-lock.json").read_bytes()).hexdigest()
    (remotion / "node_modules/react").mkdir(parents=True)  # an earlier run left this empty
    (remotion / "node_modules/.astrid-package-lock.sha256").write_text(lock_hash + "\n")
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    calls = []

    def run(argv, *, cwd, extra_env):
        calls.append(argv)
        if "ci" in argv:
            _complete(cwd, ["node_modules/react"])

    backend.run = run
    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is True
    assert any("ci" in argv for argv in calls)
    assert (remotion / "node_modules/.astrid-package-lock.sha256").read_text() == lock_hash + "\n"
    assert backend.outcomes["renderer"] == {"ok": True}


def test_complete_tree_with_matching_marker_is_reused(tmp_path):
    agent, remotion = _lock(tmp_path, {
        "node_modules/react": {"version": "18.0.0"},
        "node_modules/@remotion/cli": {"version": "1.0.0"},
    })
    lock_hash = hashlib.sha256((remotion / "package-lock.json").read_bytes()).hexdigest()
    _complete(remotion, ["node_modules/react", "node_modules/@remotion/cli"])
    (remotion / "node_modules/.astrid-package-lock.sha256").write_text(lock_hash + "\n")
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    calls = []
    backend.run = lambda argv, *, cwd, extra_env: calls.append(argv)

    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is True
    assert calls == [["node", remotion / "scripts" / "fetch-shared-browser.mjs"]]


def test_other_architecture_package_is_not_required(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {
        "node_modules/@x/arm": {"version": "1.0.0", "os": ["linux"], "cpu": ["arm64"], "optional": True},
        "node_modules/@x/x64": {"version": "1.0.0", "os": ["linux"], "cpu": ["x64"], "optional": True},
    })
    assert _remotion_tree_gaps(remotion, ("linux", "x64", "glibc")) == ["@x/x64"]
    _package(remotion, "node_modules/@x/x64")
    assert _remotion_tree_gaps(remotion, ("linux", "x64", "glibc")) == []


def test_libc_rule_is_honoured(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {
        "node_modules/@x/musl": {"version": "1.0.0", "os": ["linux"], "cpu": ["x64"], "libc": ["musl"]},
    })
    assert _remotion_tree_gaps(remotion, ("linux", "x64", "glibc")) == []
    assert _remotion_tree_gaps(remotion, ("linux", "x64", "musl")) == ["@x/musl"]


def test_nested_node_modules_key_is_checked(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {
        "node_modules/a": {"version": "1.0.0"},
        "node_modules/a/node_modules/b": {"version": "2.0.0"},
    })
    _package(remotion, "node_modules/a")
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == ["a/node_modules/b"]
    _package(remotion, "node_modules/a/node_modules/b", version="2.0.0")
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == []


def test_dev_entries_are_required(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {"node_modules/typescript": {"version": "5.0.0", "dev": True}})
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == ["typescript"]
    _package(remotion, "node_modules/typescript", version="5.0.0")
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == []


def test_version_and_declared_entry_must_match(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {
        "node_modules/wrong": {"version": "2.0.0"},
        "node_modules/no-entry": {"version": "1.0.0"},
    })
    _package(remotion, "node_modules/wrong", version="1.0.0")
    _package(remotion, "node_modules/no-entry", main="lib/missing.js", files=False)
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == ["wrong", "no-entry"]


def test_bin_links_are_required_when_a_package_declares_bins(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {"node_modules/tool": {"version": "1.0.0", "bin": {"tool": "bin.js"}}})
    _package(remotion, "node_modules/tool")
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == [".bin links"]
    (remotion / "node_modules/.bin").mkdir()
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == []


def test_unpatched_renderer_is_incomplete(tmp_path):
    from astrid.installer.backend import _remotion_tree_gaps

    _agent, remotion = _lock(tmp_path, {})
    (remotion / "patches").mkdir()
    patched = b"patched\n"
    (remotion / "patches/test.json").write_text(json.dumps({
        "package": "left-pad", "version": "1.0.0",
        "files": [{"path": "index.js", "before_sha256": "0", "after_sha256": hashlib.sha256(patched).hexdigest()}],
    }))
    _package(remotion, "node_modules/left-pad")  # the postinstall patch never ran
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == ["left-pad/index.js (postinstall patch)"]
    (remotion / "node_modules/left-pad/index.js").write_bytes(patched)
    assert _remotion_tree_gaps(remotion, ("linux", "x64", None)) == []


def test_failed_browser_fetch_names_the_visualize_loss_and_its_own_repair(tmp_path):
    from astrid.installer.outcome import Outcome, receipt_warnings

    agent, remotion = _lock(tmp_path, {"node_modules/react": {"version": "18.0.0"}})
    _complete(remotion, ["node_modules/react"])
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    backend.run = Mock(side_effect=subprocess.CalledProcessError(1, "fetch"))  # only the browser fetch runs
    (remotion / "node_modules/.astrid-package-lock.sha256").write_text(
        hashlib.sha256((remotion / "package-lock.json").read_bytes()).hexdigest() + "\n")
    assert backend.install_remotion_dependencies(
        agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js")) is True
    assert backend.outcomes["renderer"] == {"ok": False, "reason": "browser"}
    lines = Outcome("repaired", 12, warnings=receipt_warnings(backend.outcomes)).headline()
    assert lines[0] == "Astrid is installed and running (Release 12), but timelines visualize and renders are unavailable."
    assert "rebuilds" not in lines[1] and "install line again" in lines[1]


def test_rolled_back_renderer_result_is_not_reported(tmp_path):
    from astrid.installer.cli import _receipt_outcomes
    from astrid.installer.outcome import receipt_warnings

    backend = SimpleNamespace(outcomes={"renderer": {"ok": False, "reason": "tree"}})
    installer = SimpleNamespace(outcomes={"skills": {"ok": True}}, backend=backend)
    assert receipt_warnings(_receipt_outcomes(installer)) == ("renderer-incomplete",)
    assert receipt_warnings(_receipt_outcomes(installer, rolled_back=True)) == ()


def test_each_run_states_its_own_renderer_result(tmp_path):
    agent, remotion = _lock(tmp_path, {})
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    backend.run = Mock(side_effect=subprocess.CalledProcessError(1, "npm"))
    backend.install_remotion_dependencies(agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js"))
    assert backend.outcomes["renderer"]["ok"] is False
    backend.run = lambda argv, *, cwd, extra_env: (cwd / "node_modules").mkdir(exist_ok=True)
    backend.install_remotion_dependencies(agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js"))
    assert backend.outcomes["renderer"] == {"ok": True}


def test_missing_renderer_package_is_named_only_for_node_modules_or_bare_specifiers():
    from astrid.packs.rendering.rendering.renderers.remotion.run import _missing_renderer_package

    esm = ("Error [ERR_MODULE_NOT_FOUND]: Cannot find package "
           "'/x/remotion/node_modules/react/index.js' imported from /x/a.mjs")
    message = _missing_renderer_package(esm)
    assert "package react is missing" in message and "install line again" in message
    assert _missing_renderer_package("Error: Cannot find module 'left-pad'") is not None
    assert "package @scope/pkg" in _missing_renderer_package("Cannot find module '@scope/pkg/x'")
    assert _missing_renderer_package("Error: Cannot find module './local.js'") is None
    assert _missing_renderer_package("Error: Cannot find module '/abs/file.js'") is None
    assert _missing_renderer_package("Error: something else") is None


def test_failed_renderer_step_reaches_the_receipt_through_the_cli(tmp_path):
    from astrid.installer.cli import _receipt_outcomes
    from astrid.installer.outcome import receipt_warnings

    agent, remotion = _lock(tmp_path, {"node_modules/react": {"version": "18.0.0"}})
    backend = Backend(tmp_path, tmp_path / "log", Path("uv"))
    backend.run = lambda argv, *, cwd, extra_env: None  # npm exits 0 and writes nothing
    backend.install_remotion_dependencies(agent, {"ASTRID_NODE_EXECUTABLE": "node"}, Path("npm-cli.js"))
    installer = SimpleNamespace(outcomes={"skills": {"ok": True}}, backend=backend)

    assert receipt_warnings(_receipt_outcomes(installer)) == ("renderer-incomplete",)
    assert receipt_warnings(_receipt_outcomes(None)) == ()


def test_incomplete_tree_warning_reaches_the_success_headline():
    from astrid.installer.outcome import Outcome, receipt_warnings

    headline = Outcome("repaired", 12, warnings=receipt_warnings({"renderer": {"ok": False, "reason": "tree"}})).headline()
    assert headline[0] == "Astrid is installed and running (Release 12), but frame looks and renders are unavailable."
    assert "install line again" in headline[1]
