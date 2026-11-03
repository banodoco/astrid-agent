"""Lane collection (`require_module`, modules of a deselected lane) and the Runtime locator (testing plan, R-6 and UA.3).

The skip guard itself is tests/test_skip_guard.py."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(
    tmp_path: Path, source: str, *args: str, extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(
        "from tests.lanes import pytest_collection_modifyitems, pytest_configure, pytest_ignore_collect  # noqa: F401\n"
    )
    (tmp_path / "test_case.py").write_text(source)
    env = {key: value for key, value in os.environ.items() if key not in ("CI", "PYTEST_ADDOPTS", "ASTRID_CI_JOB")}
    env["PYTHONPATH"] = str(REPO)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(tmp_path),
         "--confcutdir", str(tmp_path), *args, str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False,
    )


DEFAULT = ("-m", "not integration and not opt_in")
HEAVY = ("-m", "integration or opt_in")
REQUIRE = (
    "import pytest\nfrom tests.lanes import require_module\n\npytestmark = pytest.mark.integration\n"
    "require_module('astrid_no_such_dependency')\n\ndef test_x():\n    pass\n"
)


def test_a_module_of_a_lane_the_run_deselects_is_not_imported_and_fails_in_the_run_that_selects_it(tmp_path: Path) -> None:
    default = _run(tmp_path, REQUIRE, *DEFAULT)
    assert default.returncode == 5 and "skipped" not in default.stdout, default.stdout + default.stderr  # 5: nothing collected
    heavy = _run(tmp_path, REQUIRE, *HEAVY)
    assert heavy.returncode != 0 and "astrid_no_such_dependency" in heavy.stdout, heavy.stdout + heavy.stderr


NO_RUNTIME = (
    "import pytest\nfrom tests.helpers.runtime import runtime_checkout\n\npytestmark = pytest.mark.integration\n"
    "runtime_checkout()\n\ndef test_x():\n    pass\n"
)


def test_a_missing_runtime_is_not_looked_for_in_the_default_lane_and_fails_in_the_integration_lane(tmp_path: Path) -> None:
    missing = {"ASTRID_RUNTIME_CHECKOUT": str(tmp_path / "no-runtime")}
    default = _run(tmp_path, NO_RUNTIME, *DEFAULT, extra_env=missing)
    assert default.returncode == 5 and "skipped" not in default.stdout, default.stdout + default.stderr
    heavy = _run(tmp_path, NO_RUNTIME, *HEAVY, extra_env=missing)
    assert heavy.returncode != 0 and "no Runtime checkout" in heavy.stdout, heavy.stdout + heavy.stderr


def test_the_runtime_locator_takes_the_override_and_never_falls_through_from_it(tmp_path: Path, monkeypatch) -> None:
    from tests.helpers import runtime

    checkout = tmp_path / "runtime"
    (checkout / "runtime_protocol").mkdir(parents=True)
    monkeypatch.setenv(runtime.ENV, str(checkout))
    assert runtime.locate_runtime() == checkout.resolve()
    monkeypatch.setenv(runtime.ENV, str(tmp_path / "elsewhere"))
    assert runtime.locate_runtime() is None


def test_the_runtime_locator_reads_the_shared_dependency_pin() -> None:
    import json

    from tests.helpers import runtime

    assert runtime.vendored_runtime_commit() == json.loads((REPO / "runtime.lock.json").read_text())["source_commit"]


def test_the_runtime_locator_runs_the_runtime_as_of_a_commit(tmp_path: Path, monkeypatch) -> None:
    from tests.helpers import runtime

    checkout = tmp_path / "runtime"
    (checkout / "runtime_protocol").mkdir(parents=True)
    marker = checkout / "runtime_protocol" / "__init__.py"
    marker.write_text("VERSION = 1\n")
    git = ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for command in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "one"]):
        subprocess.run([*git, *command], check=True, capture_output=True)
    commit = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    marker.write_text("VERSION = 2\n")  # a later working-tree edit must not reach the pinned copy
    monkeypatch.setenv(runtime.ENV, str(checkout))
    pinned = runtime.runtime_checkout(commit)
    assert (pinned / "runtime_protocol" / "__init__.py").read_text() == "VERSION = 1\n"
    assert runtime.runtime_checkout(commit) == pinned
    assert runtime.runtime_checkout() == checkout.resolve()
