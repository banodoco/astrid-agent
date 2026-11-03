"""The skip guard of tests/lanes.py: no invisible skips.

Each case runs a throwaway test module in a fresh pytest process (pytester) whose conftest carries the real
lane hooks. A skip passes only when a registry lane declares it, and it is then listed by test id.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests import lanes

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parents[1]
HOOKS = "from tests.lanes import pytest_collection_modifyitems, pytest_configure, pytest_ignore_collect  # noqa: F401\n"
UBUNTU_JOB = lanes.UBUNTU


@pytest.fixture
def run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in ("CI", "PYTEST_ADDOPTS", "ASTRID_CI_JOB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    pytester.makeconftest(HOOKS)
    bare_path = tmp_path / "empty-path"
    bare_path.mkdir()

    def go(source: str, *args: str, job: str = "", bare_path_env: bool = False) -> pytest.RunResult:
        pytester.makepyfile(test_case=source)
        if job:
            monkeypatch.setenv("ASTRID_CI_JOB", job)
        if bare_path_env:
            monkeypatch.setenv("PATH", str(bare_path))
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-o", "addopts=", *args)

    return go


def test_an_undeclared_skip_in_a_test_body_fails_the_run(run) -> None:
    result = run("import pytest\n\ndef test_x():\n    pytest.skip('flaky here')\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*test_case.py::test_x*undeclared skip (*flaky here)*"])


def test_an_undeclared_skipif_mark_fails_the_run(run) -> None:
    result = run("import pytest\n\n@pytest.mark.skipif(True, reason='nope')\ndef test_x():\n    pass\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*undeclared skip (*nope)*"])


def test_an_importorskip_in_a_test_body_fails_the_run(run) -> None:
    result = run("import pytest\n\ndef test_x():\n    pytest.importorskip('astrid_no_such_dependency')\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*test_case.py::test_x*undeclared skip (*astrid_no_such_dependency*"])


def test_a_module_level_importorskip_fails_the_run(run) -> None:
    result = run("import pytest\n\npytest.importorskip('astrid_no_such_dependency')\n\ndef test_x():\n    pass\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*module-level skip hides its tests*"])


def test_a_skip_that_pretends_to_be_declared_fails_the_run(run) -> None:
    result = run("import pytest\n\ndef test_x():\n    pytest.skip('[declared:no_such_lane] trust me')\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*undeclared skip*"])


def test_a_copied_reason_on_an_unregistered_node_fails_the_run(run) -> None:
    result = run("import pytest\n\ndef test_x():\n    pytest.skip('[declared:ffmpeg] needs ffmpeg and ffprobe on PATH')\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*copied declared reason for unregistered lane node ffmpeg*"])


def test_a_declared_reason_must_match_the_registry_exactly(run) -> None:
    result = run("import pytest\n\n@pytest.mark.ffmpeg\ndef test_x():\n    pytest.skip('[declared:ffmpeg] a copied explanation')\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*declared ffmpeg skip has copied or altered reason*"])


def test_an_xfail_fails_the_run(run) -> None:
    result = run("import pytest\n\n@pytest.mark.xfail(reason='known gap')\ndef test_x():\n    assert False\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*an xfail hides the result*"])


def test_a_declared_platform_skip_passes_and_is_listed_by_test_id(run) -> None:
    other = "windows_native" if sys.platform != "win32" else "posix_native"
    result = run(f"import pytest\n\n@pytest.mark.{other}\ndef test_x():\n    assert False\n\ndef test_y():\n    pass\n")
    assert result.ret == 0
    result.stdout.fnmatch_lines([
        "*declared skips (1)*",
        f"SKIPPED [[]{other}] test_case.py::test_x: *",
    ])


def test_windows_native_skips_are_declared_and_run_in_ci(run) -> None:
    result = run("import pytest\n\n@pytest.mark.windows_native\ndef test_x():\n    assert False\n\ndef test_y():\n    pass\n")
    assert result.ret == 0
    if sys.platform != "win32":  # on Windows the lane's tests run, so nothing is skipped or listed
        result.stdout.fnmatch_lines(["*declared skips (1)*", "SKIPPED [[]windows_native] test_case.py::test_x: needs native Windows APIs"])


def test_a_declared_dependency_skip_passes_where_no_job_provides_it_and_is_listed(run) -> None:
    result = run("import pytest\n\n@pytest.mark.ffmpeg\ndef test_x():\n    pass\n\ndef test_y():\n    pass\n", bare_path_env=True)
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*declared skips (1)*", "SKIPPED [[]ffmpeg] test_case.py::test_x: *"])


def test_a_missing_dependency_fails_in_the_job_that_is_meant_to_provide_it(run) -> None:
    result = run("import pytest\n\n@pytest.mark.ffmpeg\ndef test_x():\n    pass\n", job=UBUNTU_JOB, bare_path_env=True)
    assert result.ret != 0
    result.stdout.fnmatch_lines([f"*the ffmpeg lane's dependency is missing but the {UBUNTU_JOB} job provides it*"])


def test_a_missing_dependency_fails_in_a_lane_the_run_selected_by_name(run) -> None:
    source = "import pytest\n\n@pytest.mark.remotion\ndef test_x():\n    pass\n"
    result = run(source, "-m", "remotion", bare_path_env=True)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*the remotion lane's dependency is missing but this run selected the lane*"])


def test_a_test_in_several_lanes_is_one_declared_skip_where_one_of_them_does_not_hold(run) -> None:
    result = run("import pytest\n\n@pytest.mark.posix_native\n@pytest.mark.linux_native\n@pytest.mark.darwin_native\ndef test_x():\n    pass\n\ndef test_y():\n    pass\n", "-m", "darwin_native or linux_native or not posix_native")
    # no OS is POSIX, Linux and macOS at once, so one lane is unmet; that is a single declared skip, never a failure
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*declared skips (1)*"])


def test_an_unregistered_marker_is_an_error_under_strict_markers(run) -> None:
    result = run("import pytest\n\n@pytest.mark.ffmpeg_typo\ndef test_x():\n    pass\n", "--strict-markers")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*'ffmpeg_typo' not found in `markers` configuration option*"])


def test_a_run_in_which_every_test_is_skipped_or_deselected_fails(run) -> None:
    other = "windows_native" if sys.platform != "win32" else "posix_native"
    skipped = run(f"import pytest\n\n@pytest.mark.{other}\ndef test_x():\n    pass\n")
    assert skipped.ret != 0
    skipped.stdout.fnmatch_lines(["*no test passed in this run*"])
    deselected = run("def test_x():\n    pass\n", "-k", "nothing_matches")
    assert deselected.ret != 0  # pytest's own "no tests collected"


def test_a_module_of_a_deselected_lane_is_not_imported_unless_a_test_in_it_names_a_selected_lane(run, pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_other="import pytest\n\npytestmark = pytest.mark.workstation\nimport astrid_no_such_second_checkout\n\ndef test_z():\n    pass\n")
    pytester.makepyfile(test_both="import pytest\n\npytestmark = pytest.mark.workstation\n\n@pytest.mark.gpu\ndef test_w():\n    pass\n")
    result = run("def test_x():\n    pass\n", "-m", "gpu or not workstation")
    assert result.ret == 0, result.stdout.str()  # test_other.py (workstation only) was never imported; test_both.py was, and its gpu test ran
    result.stdout.fnmatch_lines(["*2 passed*"])


def test_tests_deselected_by_a_lane_are_named_under_a_header_that_says_who_runs_the_lane(run) -> None:
    result = run("import pytest\n\n@pytest.mark.gpu\ndef test_manual():\n    pass\n\n@pytest.mark.remotion\ndef test_heavy():\n    pass\n\ndef test_x():\n    pass\n", "-m", "not gpu and not remotion")
    assert result.ret == 0
    result.stdout.fnmatch_lines([
        "*left out of this run by lane (2 entries)*",
        "[[]gpu] NO CI job runs this lane*",
        "  DESELECTED test_case.py::test_manual",
        f"[[]remotion] run by CI job {lanes.DARWIN}",
        "  DESELECTED test_case.py::test_heavy",
    ])


def test_a_module_not_imported_because_of_its_lane_is_named_with_the_lane(run, pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_hidden="import pytest\n\npytestmark = pytest.mark.workstation\n\ndef test_z():\n    pass\n")
    result = run("def test_x():\n    pass\n", "-m", "not workstation")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["[[]workstation] NO CI job runs this lane*", "  NOT IMPORTED test_hidden.py *"])


def test_a_run_with_nothing_left_out_prints_no_report(run) -> None:
    result = run("def test_x():\n    pass\n")
    assert result.ret == 0
    assert "left out of this run" not in result.stdout.str()


def test_a_module_with_a_module_lane_and_one_per_test_lane_is_imported_when_its_other_tests_are_selected(run, pytester: pytest.Pytester) -> None:
    # The reviewed bug: pytestmark = posix_native plus one @remotion test. Under "not remotion" its posix_native-only
    # test must run; judging the module by the union of every lane in the file hid it.
    pytester.makepyfile(test_mixed="import pytest\n\npytestmark = pytest.mark.posix_native\n\n@pytest.mark.remotion\ndef test_heavy():\n    pass\n\ndef test_plain():\n    pass\n")
    result = run("def test_x():\n    pass\n", "-m", "not remotion", "-v")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*test_mixed.py::test_plain PASSED*"])
    result.stdout.fnmatch_lines(["  DESELECTED test_mixed.py::test_heavy"])


def test_a_module_is_hidden_only_when_no_combination_of_its_lanes_satisfies_the_expression() -> None:
    hidden = lanes._module_is_hidden
    assert hidden("not integration", {"integration"}, {"integration"})
    assert not hidden("not remotion", {"posix_native"}, {"posix_native", "remotion"})
    assert hidden("remotion", {"posix_native"}, {"posix_native"})  # nothing in it can be a remotion test
    assert not hidden("remotion", {"posix_native"}, {"posix_native", "remotion"})


def test_a_test_removed_by_name_is_counted_and_not_attributed_to_its_lane(run) -> None:
    # -m selects the node test; --deselect (as pytest-split does) removes it. The lane did not leave it out.
    result = run("import pytest\n\n@pytest.mark.node\ndef test_node():\n    pass\n\ndef test_x():\n    pass\n", "-m", "not opt_in", "--deselect", "test_case.py::test_node")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["1 tests left out by --splits/-k/--deselect (run by other shards or excluded by name)"])
    assert "[node]" not in result.stdout.str()
    assert "DESELECTED" not in result.stdout.str()


def test_a_test_in_two_lanes_is_attributed_only_to_the_lane_that_makes_the_expression_false(run) -> None:
    result = run("import pytest\n\n@pytest.mark.gpu\n@pytest.mark.node\ndef test_both():\n    pass\n\ndef test_x():\n    pass\n", "-m", "not gpu")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*left out of this run by lane (1 entries)*", "[[]gpu] NO CI job runs this lane*", "  DESELECTED test_case.py::test_both"])
    assert "[node]" not in result.stdout.str()


def test_a_module_the_run_never_asked_for_is_not_reported_as_not_imported(run, pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_hidden="import pytest\n\npytestmark = pytest.mark.integration\n\ndef test_z():\n    pass\n")
    asked = run("def test_x():\n    pass\n", "-m", "not integration", "test_case.py")
    assert asked.ret == 0
    assert "NOT IMPORTED" not in asked.stdout.str()
    named = run("def test_x():\n    pass\n", "-m", "not integration", ".")
    assert "NOT IMPORTED test_hidden.py" in named.stdout.str()
