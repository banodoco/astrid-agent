from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True
if sys.pycache_prefix is None:
    _pycache_prefix = Path(tempfile.mkdtemp(prefix="astrid-pycache-"))
    # Only this process moves its bytecode. Do not export PYTHONPYCACHEPREFIX: children already inherit
    # PYTHONDONTWRITEBYTECODE, and the Remotion runtime validation refuses a process whose environment carries
    # that startup override (remotion_runtime._UNSUPPORTED_PYTHON_STARTUP), which in-process renders read.
    sys.pycache_prefix = str(_pycache_prefix)
    atexit.register(lambda: shutil.rmtree(_pycache_prefix, ignore_errors=True))

from astrid.core.foundation import project_paths as paths
from astrid.core.pack import source_setup
from astrid.core.subprocess_env import (
    TASK_ITEM_ID_ENV,
    TASK_ITERATION_ENV,
    TASK_PROJECT_ENV,
    TASK_RUN_ID_ENV,
    TASK_STEP_ID_ENV,
)
from tests.lanes import pytest_collection_modifyitems, pytest_configure, pytest_ignore_collect  # noqa: F401  (lanes, skip registry and guard, fd guard)
from tests.process_groups import ProcessGroups


@pytest.fixture
def owned_process_groups():
    """Own process groups explicitly launched by this test until teardown."""
    owner = ProcessGroups()
    try:
        yield owner
    finally:
        owner.terminate_all()


@pytest.fixture
def real_runtime(tmp_path, owned_process_groups):
    """Start a real Runtime daemon on a temp realm; the one way a test gets one (testing plan AT-1).

    ``real_runtime()`` starts the daemon at ``tmp_path/realm`` with its support root at ``tmp_path/support``.
    ``real_runtime(realm, support_root=..., **daemon_options)`` starts one at a given realm (a restart reuses
    it; the support root is made owner-only like the launcher's); ``commit=`` runs the Runtime as of that commit. The Runtime comes from ``tests.helpers.runtime``'s
    locator (``ASTRID_RUNTIME_CHECKOUT``). Every daemon is stopped at teardown.
    """
    from tests.helpers.runtime import new_runtime_daemon

    def start(realm=None, *, support_root=None, commit=None, **daemon_options):
        if realm is None:
            realm, support_root = tmp_path / "realm", support_root or tmp_path / "support"
        if support_root is not None:
            # The installed launcher keeps the support root owner-only (0o700); the host refuses to read
            # discovery evidence from a wider directory.
            Path(support_root).mkdir(parents=True, exist_ok=True)
            Path(support_root).chmod(0o700)
            daemon_options["support_root"] = support_root
        daemon = new_runtime_daemon(realm, commit=commit, **daemon_options).start()
        owned_process_groups.add_cleanup(daemon.stop)
        return daemon

    return start


def pytest_sessionfinish(session, exitstatus):
    """Fail after killing runtime daemons/hosts rooted in pytest basetemp."""
    _ = exitstatus
    import signal
    import subprocess
    import time

    try:
        basetemp = Path(session.config._tmp_path_factory.getbasetemp()).resolve()
        listing = subprocess.run(["ps", "-axo", "pid=,pgid=,command="], capture_output=True, text=True, check=True).stdout
    except (AttributeError, OSError, subprocess.CalledProcessError) as exc:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"WARNING: daemon/host session guard could not inspect processes: {exc}")
        return
    leaked = []
    for line in listing.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, pgid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        command = fields[2]
        is_runtime = "runtime_protocol" in command and "start" in command and "--root" in command
        is_host = "generic_host" in command and "--support-root" in command
        if not (is_runtime or is_host):
            continue
        tokens = command.split()
        roots = []
        for option in ("--root", "--support-root"):
            if option in tokens and tokens.index(option) + 1 < len(tokens):
                roots.append(tokens[tokens.index(option) + 1].strip("'\""))
            roots.extend(token.split("=", 1)[1].strip("'\"") for token in tokens if token.startswith(option + "="))
        if any(_under_basetemp(root, basetemp) for root in roots):
            leaked.append((pid, pgid, command))
    if not leaked:
        return
    groups = {pgid for _, pgid, _ in leaked if pgid != os.getpgrp()}
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            # PermissionError: seccomp sandboxes (e.g. OpenShell) deny process-group
            # kills; still report the leak below instead of losing the summary.
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(_group_exists(pgid) for pgid in groups):
        time.sleep(0.02)
    for pgid in groups:
        if _group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    session.config.pluginmanager.get_plugin("terminalreporter").write_line(
        f"ERROR: killed daemon/host processes left under pytest basetemp: {leaked!r}"
    )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _under_basetemp(candidate: str, parent: Path) -> bool:
    try:
        return Path(candidate).resolve().is_relative_to(parent)
    except (OSError, ValueError):
        return False


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

if "ASTRID_TIMELINE_COMPOSITION_SRC" not in os.environ:
    _package_src = Path(tempfile.mkdtemp(prefix="astrid-timeline-composition-src-"))
    os.environ["ASTRID_TIMELINE_COMPOSITION_SRC"] = str(_package_src)
    atexit.register(lambda: shutil.rmtree(_package_src, ignore_errors=True))


# v10 prep (Fix #1, brief caveat): each pack's ``run.py`` now calls
# ``guard_canonical_entrypoint()`` at module-top, so any test that imports
# ``astrid.packs.<X>.run`` would otherwise exit with code 2. Set the marker
# env var once at conftest load so test collection (and any internal
# import chain that brushes a pack's run module) flows through cleanly.
# Production callers continue to set this in the subprocess env via the
# canonical runners.
os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")


def _clear_task_env() -> None:
    """Scrub raw task-run env that tests may mutate outside monkeypatch."""

    for name in (
        TASK_RUN_ID_ENV,
        TASK_PROJECT_ENV,
        TASK_STEP_ID_ENV,
        TASK_ITEM_ID_ENV,
        TASK_ITERATION_ENV,
    ):
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def _sandboxed_home_and_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Sandbox home, managed pack sources, workspace config, and projects.

    Every test gets tmp-backed state roots so no test writes into the real
    ``~/.astrid`` or the user's projects directory. The legacy session
    autouse seed (identity and session) was retired
    with the task-mode session layer.
    """

    _clear_task_env()
    # This fixture is function-scoped, so keep all state roots under the
    # function-scoped tmp_path. Using tmp_path_factory here retained three
    # roots for every test until session end and exhausted disk in the full
    # 7,000+ test suite before the installed-artifact lanes could start.
    astrid_home = tmp_path / "astrid-home"
    home = tmp_path / "home"
    xdg_state_home = tmp_path / "xdg-state"
    xdg_data_home = tmp_path / "xdg-data"
    projects_root = tmp_path / "projects"
    workspace_config_dir = tmp_path / "workspace-config"
    for root in (astrid_home, home, xdg_state_home, xdg_data_home, projects_root, workspace_config_dir):
        root.mkdir()
    monkeypatch.setenv("ASTRID_HOME", str(astrid_home))
    # Child processes may intentionally receive only HOME after the subprocess
    # environment filter removes XDG/source selectors. Keep that fallback local.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASTRID_WORKSPACE_CONFIG_DIR", str(workspace_config_dir))
    # Managed pack discovery reads these XDG roots directly; ASTRID_HOME does
    # not redirect pack-sources.json or the managed pack cache. Ignore ambient
    # source selectors so a developer's active ~/.local/state inventory cannot
    # leak into otherwise isolated tests. Tests that exercise source setup can
    # still opt in by setting these values after this autouse fixture runs.
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
    for name in (
        source_setup.SOURCE_DECLARATIONS_ENV,
        source_setup.SOURCE_STATE_ENV,
        source_setup.SOURCE_DATA_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    # Seed PROJECTS_ROOT to a tmp dir so tests never touch the real
    # projects root. Tests that need their own projects-root override this
    # with their own monkeypatch.setenv.
    monkeypatch.setenv(paths.PROJECTS_ROOT_ENV, str(projects_root))
    yield
    _clear_task_env()
