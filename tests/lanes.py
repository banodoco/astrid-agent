"""Test lanes and the skip registry ("we shouldn't have invisible skips").

One registry, ``LANES``, declares every reason a test may be left out of a run. A test opts in with the
marker that has the lane's name (``@pytest.mark.ffmpeg``, ``@pytest.mark.posix_native``); it never calls
``pytest.skip`` or ``skipif`` itself (tests/test_skip_policy.py scans for that). A lane is one of:

* ``platform``: the code cannot run on another OS. Off that OS the test is skipped, and the skip is declared.
* ``dependency``: the test needs a tool a CI job provides (ffmpeg, Node, the Remotion install). Where the
  dependency is missing the test is skipped and the skip is declared, EXCEPT in a job that is meant to
  provide it (the job's ``ASTRID_CI_JOB`` is in the lane's ``jobs``), and in a lane the run selected by
  name (``default_off``): there a missing dependency is a failure.
* ``scheduled``: needs nothing the other lanes do not name; too slow for every push, so a scheduled job runs it.
* ``manual``: needs something no CI job has (credentials, a GPU, the owner's workstation files). Always ``default_off``,
  so the default run deselects these tests; running one by hand with ``-m`` fails, never skips, when its
  dependency is missing.

``default_off`` lanes are deselected by ``addopts`` in pyproject.toml (tests/test_lane_registry.py keeps the two
in step). Every non-manual lane names the CI jobs that run it, and tests/test_lane_registry.py checks those jobs
in .github/workflows/ci.yml: they set ``ASTRID_CI_JOB``, provision the dependency and select the lane.

The guard (``_SkipGuard``) records every skip by test id, and fails a run in which no test passed. A skip that did not come from a registry lane, a
``pytest.importorskip`` anywhere, a module-level skip at collection and every xfail turn into failures; the
terminal summary lists the declared skips by name.

Modules that wholly belong to a ``default_off`` lane (``pytestmark = pytest.mark.<lane>``) are not collected
in a run that deselects the lane, so their import-time needs (a second checkout) never run there.

Leaked file descriptors (testing plan LEAK.2). The run fails if the pytest process
ends holding more than FD_LEAK_ALLOWANCE descriptors beyond what it held at start.
(Leaked daemon and host process groups are caught by conftest's session guard.)
"""

from __future__ import annotations

import ast
import importlib
import itertools
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest

FD_LEAK_ALLOWANCE = 64
REPO = Path(__file__).resolve().parents[1]

UBUNTU = "unit-shard-ubuntu-latest"
DARWIN = "darwin-native"
INTEGRATION = "lanes-integration"  # .github/workflows/lanes.yml
OPT_IN = "lanes-opt-in"  # .github/workflows/lanes.yml
INSTALLED = "lanes-installed"  # .github/workflows/lanes.yml


def has_media_tools() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def has_runtime() -> bool:
    from tests.helpers.runtime import locate_runtime

    return locate_runtime() is not None


def has_codex() -> bool:
    return bool(shutil.which("codex"))


def has_font() -> bool:
    from astrid.packs.rendering.rendering.renderers.ffmpeg.text import _resolve_font_path

    return _resolve_font_path(False) is not None and _resolve_font_path(True) is not None


def has_chrome() -> bool:
    root = REPO / "remotion" / "node_modules" / ".remotion" / "chrome-headless-shell"
    return any(path.is_file() and os.access(path, os.X_OK) for path in root.glob("*/*/chrome-headless-shell"))


def has_remotion() -> bool:
    return bool(shutil.which("node")) and (REPO / "remotion" / "node_modules").is_dir() and has_media_tools()


def has_installed_astrid() -> bool:
    """A non-editable Astrid in this interpreter's site-packages, the installed host profile, and a provisioned Remotion project.

    This is what tests/helpers/h3_cpu_runtime.py binds to: ``_INSTALLED_EXECUTION_ROOT`` is the interpreter's purelib, and
    ``ASTRID_HOST_EXECUTION_PROFILE=installed`` puts it first on sys.path so ``astrid`` resolves there, not to the checkout.
    """
    import sysconfig

    installed = Path(sysconfig.get_path("purelib")) / "astrid" / "__init__.py"
    project = os.environ.get("ASTRID_REMOTION_PROJECT_DIR", "")
    return (
        installed.is_file()
        and os.environ.get("ASTRID_HOST_EXECUTION_PROFILE") == "installed"
        and bool(project)
        and (Path(project) / "node_modules").is_dir()
        and has_media_tools()
    )


@dataclass(frozen=True)
class Lane:
    kind: str  # "platform" | "dependency" | "scheduled" | "manual"
    reason: str
    available: Callable[[], bool]
    jobs: tuple[str, ...] = ()  # the CI jobs (ASTRID_CI_JOB) that run these tests where `available` holds
    default_off: bool = False  # deselected by addopts; a run that selects the lane must find its dependency
    provision: str = ""  # text each job in `jobs` must contain to provide the dependency
    no_ci: str = ""  # a plain reason no CI job runs this lane (then `jobs` is empty); the one alternative to a job, besides kind "manual"

    @property
    def requires_dependency(self) -> bool:
        return self.kind in ("dependency", "manual")


def _never() -> bool:
    return False


LANES: dict[str, Lane] = {
    "posix_native": Lane("platform", "needs POSIX-only process or file descriptor APIs", lambda: os.name == "posix", (UBUNTU,)),
    "non_root": Lane("platform", "needs a non-root user (chmod 000 does not block root)", lambda: not hasattr(os, "geteuid") or os.geteuid() != 0, (UBUNTU,)),
    "linux_native": Lane("platform", "needs Linux-only /proc or pidfd APIs, including the sandbox process-control fallback", lambda: sys.platform.startswith("linux"), (UBUNTU,)),
    "windows_native": Lane("platform", "needs native Windows APIs", lambda: sys.platform == "win32", ("windows-native-and-installer",)),
    "windows_installer": Lane("platform", "needs native Windows installer APIs", lambda: sys.platform == "win32", ("windows-native-and-installer",)),
    "darwin_native": Lane("platform", "needs native macOS APIs", lambda: sys.platform == "darwin", (DARWIN,), default_off=True),
    "ffmpeg": Lane("dependency", "needs ffmpeg and ffprobe on PATH", has_media_tools, (UBUNTU,), provision="ffmpeg"),
    "node": Lane("dependency", "needs Node on PATH", lambda: bool(shutil.which("node")), (UBUNTU,), provision="setup-node"),
    "font": Lane("dependency", "needs a system TTF font (DejaVu Sans on Linux, Arial on macOS)", has_font, (UBUNTU,), provision="fonts-dejavu-core"),
    "chrome": Lane(
        "dependency", "needs the provisioned Remotion headless Chromium (npx remotion browser ensure)", has_chrome,
        (DARWIN,), default_off=True, provision="remotion browser ensure",
    ),
    "remotion": Lane(
        "dependency", "needs Node, ffmpeg and the Remotion install (npm ci --prefix remotion)", has_remotion,
        (DARWIN,), default_off=True, provision="npm ci --prefix remotion",
    ),
    "integration": Lane(
        "dependency", "needs a real Runtime checkout (ASTRID_RUNTIME_CHECKOUT) and, per test, VibeComfy at its pin, the Remotion install, ffmpeg or the network",
        has_runtime, (INTEGRATION,), default_off=True, provision="astrid-runtime",
    ),
    "installed": Lane(
        "dependency",
        "needs a non-editable Astrid wheel in the interpreter's site-packages (ASTRID_HOST_EXECUTION_PROFILE=installed) and a provisioned Remotion project (ASTRID_REMOTION_PROJECT_DIR)",
        has_installed_astrid, (INSTALLED,), default_off=True, provision="installed-venv",
    ),
    "opt_in": Lane(
        "scheduled", "slow tests the per-push run leaves to the nightly lanes workflow (the Runtime checkout, VibeComfy, ffmpeg and the Remotion install)",
        lambda: True, (OPT_IN,), default_off=True, provision="npx remotion browser ensure",
    ),
    "codex_cli": Lane(
        "dependency", "needs the macOS Codex CLI on PATH (npm install -g @openai/codex)", has_codex,
        (DARWIN,), default_off=True, provision="@openai/codex",
    ),
    "workstation": Lane("manual", "needs files only the owner's workstation holds (untracked runner scripts, local run output, saved acceptance artifacts, approved readiness profiles); no CI job", _never, default_off=True),
    "app_checkout": Lane("manual", "needs a banodoco/astrid-app checkout with npm ci done (ASTRID_APP_CHECKOUT), unpinned until the owner picks a commit; no CI job", _never, default_off=True),
    "omp": Lane("manual", "needs an OMP source checkout (ASTRID_OMP_TEST_PACKAGE_ROOT) and Bun; no CI job", _never, default_off=True),
    "gpu": Lane("manual", "needs ComfyUI with model weights on a GPU machine; no CI job", _never, default_off=True),
}
DEFAULT_OFF = tuple(name for name, lane in LANES.items() if lane.default_off)
DECLARED = re.compile(r"\[declared:([a-z_]+)\]")


def _open_fds() -> int | None:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return None


class _FdGuard:
    def __init__(self) -> None:
        self.start = _open_fds()

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        end = _open_fds()
        if self.start is None or end is None or end - self.start <= FD_LEAK_ALLOWANCE:
            return
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"ERROR: the test session leaked {end - self.start} file descriptors ({self.start} -> {end})")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED



def current_job() -> str:
    return os.environ.get("ASTRID_CI_JOB", "")


def require_module(name: str) -> ModuleType:
    """Import a module that only a ``default_off`` lane provides (a second checkout or an optional library).

    Missing is an error: the module that calls this is wholly in that lane and is not collected unless the run selects it.
    """
    return importlib.import_module(name)


def unavailable(message: str) -> NoReturn:
    """A second checkout is missing: the tests that need it are in a lane the run selected, so this fails."""
    pytest.fail(message, pytrace=False)


def _lane_names(item) -> list[str]:
    seen: list[str] = []
    for mark in item.iter_markers():
        if mark.name in LANES and mark.name not in seen:
            seen.append(mark.name)
    return seen


def _declared_skip(name: str) -> pytest.MarkDecorator:
    return pytest.mark.skip(reason=f"[declared:{name}] {LANES[name].reason}")


class _SkipGuard:
    """Every skip is declared by a registry lane and reported by test id; anything else fails the run."""

    def __init__(self) -> None:
        self.declared: list[tuple[str, str, str]] = []
        self.deselected: list[tuple[tuple[str, ...], str]] = []  # (lanes that made -m false, test id)
        self.unmarked = 0  # tests with no lane mark that -m is false for
        self.elsewhere = 0  # tests -m would select, removed by pytest-split, -k or --deselect
        self.ignored: dict[str, str] = {}  # module path -> lane that hid it
        self.expression = ""

    def pytest_deselected(self, items):
        for item in items:
            held = _deselecting_lanes(item, self.expression)
            if held is None:
                self.elsewhere += 1
            elif held:
                self.deselected.append((tuple(held), item.nodeid))
            else:
                self.unmarked += 1

    def _judge(self, report, where: str, item=None) -> None:
        if hasattr(report, "wasxfail"):
            del report.wasxfail  # pytest does not count a failed report that still carries it
            report.outcome = "failed"
            report.longrepr = f"{where}: an xfail hides the result. Fix the test or delete it."
            return
        if not report.skipped:
            return
        reason = str(report.longrepr[2] if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3 else report.longrepr)
        match = DECLARED.search(reason)
        lane = LANES.get(match.group(1)) if match else None
        if lane is None:
            report.outcome = "failed"
            report.longrepr = (
                f"{where}: undeclared skip ({reason}).\n"
                "A skip must come from a lane in tests/lanes.py: mark the test with the lane's marker; do not call pytest.skip, "
                "skipif or importorskip. A dependency the package or lane provides is imported or run plainly."
            )
            return
        name = match.group(1)
        if item is not None and name not in _lane_names(item):
            report.outcome = "failed"
            report.longrepr = f"{where}: copied declared reason for unregistered lane node {name}; mark the test with @{name}."
            return
        declared_reason = re.sub(r"^Skipped:\s*", "", reason.replace(match.group(0), "")).strip()
        if declared_reason != lane.reason:
            report.outcome = "failed"
            report.longrepr = f"{where}: declared {name} skip has copied or altered reason {declared_reason!r}; expected {lane.reason!r}."
            return
        if lane.requires_dependency and (lane.default_off or current_job() in lane.jobs):
            report.outcome = "failed"
            where_lane = "this run selected the lane" if lane.default_off else f"the {current_job()} job provides it"
            report.longrepr = f"{where}: the {name} lane's dependency is missing but {where_lane}: {lane.reason}. A missing dependency here is a failure, not a skip."
            return
        self.declared.append((where, name, declared_reason))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        self._judge(outcome.get_result(), item.nodeid, item)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector):
        outcome = yield
        report = outcome.get_result()
        if report.skipped:
            report.outcome = "failed"
            report.longrepr = f"{collector.nodeid}: a module-level skip hides its tests. Mark the module or its tests with a lane from tests/lanes.py."

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        """A run that executed no test at all (everything skipped or deselected) is a failure, not a quiet pass."""
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if exitstatus != pytest.ExitCode.OK or reporter is None or session.config.option.collectonly:
            return
        if not reporter.stats.get("passed"):
            reporter.write_line("ERROR: no test passed in this run; a run that executes nothing is a failure")
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter):
        if self.declared:
            terminalreporter.write_sep("-", f"declared skips ({len(self.declared)})")
            for nodeid, name, reason in sorted(self.declared):
                terminalreporter.write_line(f"SKIPPED [{name}] {nodeid}: {reason}")
            no_ci = sorted((nodeid, name) for nodeid, name, _ in self.declared if LANES[name].no_ci)
            if no_ci:
                terminalreporter.write_sep("-", f"NOT RUN BY ANY CI JOB ({len(no_ci)})")
                for nodeid, name in no_ci:
                    terminalreporter.write_line(f"  NO CI {nodeid}: [{name}] {LANES[name].no_ci}")
        self._report_left_out(terminalreporter)

    def _report_left_out(self, terminalreporter) -> None:
        """Name every test the run's ``-m`` expression deselected and every module it did not import, by lane."""
        by_lane: dict[str, tuple[list[str], list[str]]] = {}
        for lane_names, nodeid in self.deselected:
            for lane in lane_names:
                by_lane.setdefault(lane, ([], []))[0].append(nodeid)
        for path, lane in self.ignored.items():
            by_lane.setdefault(lane, ([], []))[1].append(path)
        if not by_lane and not self.unmarked and not self.elsewhere:
            return
        total = sum(len(t) + len(m) for t, m in by_lane.values())
        if by_lane:
            terminalreporter.write_sep("-", f"left out of this run by lane ({total} entries)")
        for lane in sorted(by_lane):
            tests, modules = by_lane[lane]
            terminalreporter.write_line(f"[{lane}] {lane_runner(lane)}")
            for nodeid in sorted(tests):
                terminalreporter.write_line(f"  DESELECTED {nodeid}")
            for path in sorted(modules):
                terminalreporter.write_line(f"  NOT IMPORTED {path} (tests not listed; the whole module is in this lane)")
        if self.unmarked:
            terminalreporter.write_line(f"{self.unmarked} test(s) with no lane marker are not in this run's -m selection (counted, not listed; a run without that -m selection runs them)")
        if self.elsewhere:
            terminalreporter.write_line(f"{self.elsewhere} tests left out by --splits/-k/--deselect (run by other shards or excluded by name)")


def _module_lanes(path: Path) -> set[str]:
    """The lanes a module's top-level ``pytestmark`` puts every test of it in."""
    try:
        source = path.read_text(encoding="utf-8")
        if "pytestmark" not in source:
            return set()
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute) and sub.value.attr == "mark" and sub.attr in LANES:
                    names.add(sub.attr)
    return names


def _marker_expression_holds(expression: str, present: set[str]) -> bool:
    if not re.fullmatch(r"[\w\s()]*", expression):
        return True  # an expression this reader does not handle never hides a module
    def value(match: re.Match) -> str:
        word = match.group(0)
        if word in ("and", "or", "not"):
            return word
        return str(word in present)
    return bool(eval(re.sub(r"\w+", value, expression), {"__builtins__": {}}))  # noqa: S307  (only words, spaces and parentheses reach here)


def _marked_lanes(path: Path) -> set[str]:
    """Every lane a test in the module names anywhere (a decorator, ``marks=``, a class mark)."""
    try:
        return set(re.findall(r"\bmark\.(\w+)", path.read_text(encoding="utf-8"))) & set(LANES)
    except (OSError, UnicodeDecodeError):
        return set()


MAX_PER_TEST_LANES = 10  # 2**10 subsets is still instant; a module with more per-test lanes is simply collected


def _module_is_hidden(expression: str, module_lanes: set[str], per_test_lanes: set[str]) -> bool:
    """True only if the expression is false for every test the module could hold.

    A test carries the module's lanes plus any subset of the lanes named per test in the file, so the module is
    hidden only when no such combination satisfies the expression. When that cannot be told, it is collected.
    """
    extra = sorted(per_test_lanes - module_lanes)
    if len(extra) > MAX_PER_TEST_LANES:
        return False
    for size in range(len(extra) + 1):
        for subset in itertools.combinations(extra, size):
            if _marker_expression_holds(expression, module_lanes | set(subset)):
                return False
    return True


def _expression_lanes_known(expression: str) -> bool:
    return set(re.findall(r"\w+", expression)) - {"and", "or", "not"} <= set(LANES)


def lane_runner(lane: str) -> str:
    """The registry's answer to "who runs this lane": the CI jobs, or that none does."""
    jobs = LANES[lane].jobs
    if jobs:
        return "run by CI job " + ", ".join(jobs)
    if LANES[lane].no_ci:
        return f"NO CI job runs this lane ({LANES[lane].no_ci})"
    return f"NO CI job runs this lane ({LANES[lane].kind}: run it by hand with -m {lane})"


def _deselecting_lanes(item, expression: str) -> list[str] | None:
    """The lanes that made the ``-m`` expression false for ``item``; ``[]`` when it is false but no lane mark is on the test;
    ``None`` when the expression would select the test (or cannot be judged), so something else (pytest-split, -k) removed it.
    """
    names = _lane_names(item)
    if not expression or not _expression_lanes_known(expression):
        return None
    if _marker_expression_holds(expression, set(names)):
        return None
    single = [name for name in names if not _marker_expression_holds(expression, {name})]
    return single or names


def _asked_for(path: Path, config) -> bool:
    """True when the run was asked to collect ``path``: under ``config.args``, or ``testpaths`` when there are none."""
    base = Path(config.invocation_params.dir)
    roots = [(base / arg.split("::", 1)[0]).resolve() for arg in config.args]
    if not roots:
        roots = [(config.rootpath / entry).resolve() for entry in config.getini("testpaths")] or [base.resolve()]
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def pytest_ignore_collect(collection_path, config):
    """Do not import a module whose every possible test belongs to a lane this run deselects.

    Whether a module can be hidden is decided for each combination of its module-level lanes and the lanes its
    tests name one by one, so a module with ``pytestmark = pytest.mark.posix_native`` and one ``@remotion`` test
    is still imported in a run that deselects ``remotion`` (its other tests run). Each hidden module is reported.
    """
    expression = (config.getoption("markexpr", default="") or "").strip()
    if not expression or collection_path.suffix != ".py":
        return None
    lanes = _module_lanes(collection_path)
    if not lanes or not _expression_lanes_known(expression):
        return None  # not wholly in a lane, or an expression with a marker the registry does not know: never hide a module
    if not _module_is_hidden(expression, lanes, _marked_lanes(collection_path)):
        return None
    guard = config.pluginmanager.get_plugin("skip-guard")
    if guard is not None and _asked_for(collection_path, config):
        guard.expression = expression
        try:
            shown = str(collection_path.relative_to(config.rootpath))
        except ValueError:
            shown = str(collection_path)
        hiding = next((n for n in sorted(lanes) if not _marker_expression_holds(expression, {n})), sorted(lanes)[0])
        guard.ignored[shown] = hiding
    return True


def pytest_configure(config):
    for name, lane in LANES.items():
        config.addinivalue_line("markers", f"{name}: {lane.kind} lane: {lane.reason}")
    config.pluginmanager.register(_FdGuard(), "fd-leak-guard")
    guard = _SkipGuard()
    guard.expression = (config.getoption("markexpr", default="") or "").strip()
    config.pluginmanager.register(guard, "skip-guard")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        needs_checkout = set(getattr(getattr(item, "module", None), "INTEGRATION_FIXTURES", ()))
        if needs_checkout & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)
        for name in _lane_names(item):
            if not LANES[name].available() and LANES[name].kind != "manual":
                item.add_marker(_declared_skip(name))
                break
