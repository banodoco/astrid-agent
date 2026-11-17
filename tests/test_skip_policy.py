"""No test leaves itself out by hand: a skip is a lane marker from the registry in tests/lanes.py.

The runtime guard (tests/test_skip_guard.py) fails a run on any skip that no lane declares. This is its static
half: it finds the skip and xfail calls that would only fire on some other machine, so they cannot wait there to
hide a test. tests/lanes.py is the one place that may build a skip.
"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests import lanes

REPO = Path(__file__).resolve().parents[1]
REGISTRY = "tests/lanes.py"
HIDDEN = {
    "pytest.skip", "pytest.importorskip", "pytest.xfail", "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail",
    "unittest.skip", "unittest.skipIf", "unittest.skipUnless", "unittest.SkipTest", "unittest.case.SkipTest",
}
HIDDEN_METHODS = {"skipTest"}

# Owner-approved Plan 39 D10 snapshot, captured at 1148df0e. This frozen
# original set is independent of the live ceiling below; changing it requires
# another owner decision. Line numbers are deliberately not gate identities.
FROZEN_D10_GATES = frozenset({
    ("tests/core/execution/test_host_cleanup_ack_barrier.py",
     "test_native_exec_changes_token_without_changing_process_lifetime", "pytest.mark.skipif"),
    ("tests/core/generation/test_model_verification_record.py",
     "test_non_utf8_filename_hashes_then_reuses", "pytest.skip"),
    ("tests/core/generation/test_release_python.py", "<module>", "pytest.mark.skipif"),
    ("tests/core/generation/test_release_python.py",
     "test_real_recipe_bootstrap_reaches_managed_cpython_3_12_10", "pytest.mark.skipif"),
    ("tests/core/pack/test_git_environment.py", "test_planted_ssh_in_working_directory_never_runs", "pytest.skip"),
    ("tests/core/pack/test_git_environment.py",
     "test_git_source_uses_recorded_install_path_when_service_path_lacks_git", "pytest.skip"),
    ("tests/core/pack/test_identity.py",
     "test_digest_rejects_symlink_even_when_named_as_excluded_directory", "pytest.skip"),
    ("tests/scripts/test_pod_bootstrap.py",
     "test_setup_runtime_with_real_runtime_creates_workspace_once", "pytest.mark.skipif"),
    ("tests/scripts/test_pod_session.py", "video", "pytest.skip"),
    ("tests/test_installed_wheel_smoke.py", "test_fresh_wheel_cli", "pytest.skip"),
    ("tests/test_installer_pinokio_activation.py",
     "test_update_candidate_and_rollback_activate_under_retained_custody", "pytest.mark.skipif"),
    ("tests/test_installer_r0_entry.py", "test_captures_match_real_r0_evidence", "pytest.skip"),
    ("tests/test_installer_r0_stop.py", "test_symlink_lock_refused_and_left_intact", "pytest.skip"),
    ("tests/test_journey_diagnostic_checkpoint.py",
     "test_census_rejects_enumeration_failure_and_symlink", "pytest.skip"),
    ("tests/test_journey_diagnostic_checkpoint.py",
     "test_real_r0_realm_checkpoint_when_runtime_checkout_available", "pytest.skip"),
    ("tests/test_journey_windows_acl_capture.py", "test_candidate_selection_missing_multiple_and_alias", "pytest.skip"),
})

# AT5 removes each gate and its entry from this live ceiling together. Keep
# this literal separate from the frozen snapshot so additions and swaps fail.
D10_GATE_CEILING = frozenset({
    ("tests/core/execution/test_host_cleanup_ack_barrier.py",
     "test_native_exec_changes_token_without_changing_process_lifetime", "pytest.mark.skipif"),
    ("tests/core/generation/test_model_verification_record.py",
     "test_non_utf8_filename_hashes_then_reuses", "pytest.skip"),
    ("tests/core/generation/test_release_python.py", "<module>", "pytest.mark.skipif"),
    ("tests/core/generation/test_release_python.py",
     "test_real_recipe_bootstrap_reaches_managed_cpython_3_12_10", "pytest.mark.skipif"),
    ("tests/core/pack/test_git_environment.py", "test_planted_ssh_in_working_directory_never_runs", "pytest.skip"),
    ("tests/core/pack/test_git_environment.py",
     "test_git_source_uses_recorded_install_path_when_service_path_lacks_git", "pytest.skip"),
    ("tests/core/pack/test_identity.py",
     "test_digest_rejects_symlink_even_when_named_as_excluded_directory", "pytest.skip"),
    ("tests/scripts/test_pod_bootstrap.py",
     "test_setup_runtime_with_real_runtime_creates_workspace_once", "pytest.mark.skipif"),
    ("tests/scripts/test_pod_session.py", "video", "pytest.skip"),
    ("tests/test_installed_wheel_smoke.py", "test_fresh_wheel_cli", "pytest.skip"),
    ("tests/test_installer_pinokio_activation.py",
     "test_update_candidate_and_rollback_activate_under_retained_custody", "pytest.mark.skipif"),
    ("tests/test_installer_r0_entry.py", "test_captures_match_real_r0_evidence", "pytest.skip"),
    ("tests/test_installer_r0_stop.py", "test_symlink_lock_refused_and_left_intact", "pytest.skip"),
    ("tests/test_journey_diagnostic_checkpoint.py",
     "test_census_rejects_enumeration_failure_and_symlink", "pytest.skip"),
    ("tests/test_journey_diagnostic_checkpoint.py",
     "test_real_r0_realm_checkpoint_when_runtime_checkout_available", "pytest.skip"),
    ("tests/test_journey_windows_acl_capture.py", "test_candidate_selection_missing_multiple_and_alias", "pytest.skip"),
})


def hidden_gates(source: str) -> list[tuple[int, str]]:
    """The (line, dotted name) of every skip or xfail the source calls, names resolved through its imports."""
    tree = ast.parse(source)
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = [node.attr]
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                dotted = ".".join([imported.get(value.id, value.id), *reversed(parts)])
                if dotted in HIDDEN or node.attr in HIDDEN_METHODS:
                    found.append((node.lineno, dotted))
        elif isinstance(node, ast.Name) and imported.get(node.id) in HIDDEN:
            found.append((node.lineno, imported[node.id]))
    return sorted(set(found))


def gate_identities(path: str, source: str) -> list[tuple[str, str, str]]:
    """Give detected gates a path/function/API key that survives line shifts."""
    gates = hidden_gates(source)
    if not gates:
        return []
    functions: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = prefix + node.name
            first = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            functions.append((first, node.end_lineno or node.lineno, qualname))
            prefix = qualname + ".<locals>."
        elif isinstance(node, ast.ClassDef):
            prefix += node.name + "."
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(ast.parse(source))
    return [
        (path, next((name for first, last, name in reversed(functions) if first <= line <= last), "<module>"), api)
        for line, api in gates
    ]


@pytest.mark.parametrize("source", [
    "import pytest\npytest.skip('x')\n",
    "import pytest\npytest.importorskip('x')\n",
    "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test(): pass\n",
    "import pytest\n@pytest.mark.skip(reason='x')\ndef test(): pass\n",
    "import pytest\n@pytest.mark.xfail\ndef test(): pass\n",
    "import pytest\npytest.xfail('x')\n",
    "import unittest\n@unittest.skipUnless(False, 'x')\nclass T(unittest.TestCase): pass\n",
    "import unittest\nclass T(unittest.TestCase):\n    def test(self):\n        self.skipTest('x')\n",
    "import pytest as p\np.skip('x')\n",
    "from pytest import skip as omit\nomit('x')\n",
    "from unittest import SkipTest\nraise SkipTest('x')\n",
], ids=["skip", "importorskip", "skipif", "skip-mark", "xfail-mark", "xfail", "unittest", "skipTest", "alias", "from-import", "SkipTest"])
def test_the_scanner_finds_every_way_to_hide_a_test(source: str) -> None:
    assert hidden_gates(source)


def test_the_scanner_leaves_lane_markers_and_plain_strings_alone() -> None:
    source = "import pytest\n\n@pytest.mark.ffmpeg\ndef test():\n    text = 'pytest.skip is not called'\n"
    assert hidden_gates(source) == []


def _xfail_entry(today: date) -> dict[str, object]:
    return {
        "nodeid": "tests/test_example.py::test_known_gap",
        "reason": "Known, narrowly scoped failure",
        "issue": "https://github.com/banodoco/astrid-dev/issues/742",
        "owner": "maintainer",
        "expires": (today + timedelta(days=7)).isoformat(),
        "strict": True,
    }


def test_the_xfail_registry_is_valid_and_empty() -> None:
    assert lanes.DECLARED_XFAILS == {}
    assert lanes.declared_xfail_errors() == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("nodeid", "missing or invalid exact nodeid"),
        ("reason", "missing reason"),
        ("issue", "missing or invalid issue URL"),
        ("owner", "missing owner"),
        ("expires", "expiry must be a future ISO date"),
        ("past", "expiry must be a future ISO date"),
        ("far-future", "expiry must be a future ISO date"),
        ("strict", "strict must be true"),
    ],
)
def test_xfail_registry_rejects_missing_metadata_expired_or_non_strict(
    change: str, expected: str
) -> None:
    today = date(2026, 11, 16)
    entry = _xfail_entry(today)
    if change in {"nodeid", "reason", "issue", "owner", "expires"}:
        entry.pop(change)
    elif change == "past":
        entry["expires"] = (today - timedelta(days=1)).isoformat()
    elif change == "far-future":
        entry["expires"] = (today + timedelta(days=15)).isoformat()
    else:
        entry["strict"] = False

    errors = lanes.declared_xfail_errors({"known-gap": entry}, today=today)

    assert any(expected in error for error in errors)


def test_xfail_registry_rejects_more_than_ten_entries() -> None:
    today = date(2026, 11, 16)
    entries = {
        f"gap-{index}": {**_xfail_entry(today), "nodeid": f"tests/test_{index}.py::test_gap"}
        for index in range(11)
    }

    assert any("maximum is 10" in error for error in lanes.declared_xfail_errors(entries, today=today))


def test_no_test_file_skips_by_hand() -> None:
    assert len(FROZEN_D10_GATES) == 16, "the original owner-approved D10 snapshot must remain frozen"
    outside_frozen = sorted(D10_GATE_CEILING - FROZEN_D10_GATES)
    assert not outside_frozen, f"D10 ceiling entries outside the frozen D10 set: {outside_frozen}"
    offenders = []
    for root in (REPO / "tests", *sorted((REPO / "tools").glob("*/tests"))):
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if relative == REGISTRY or ".venv" in path.parts or "node_modules" in path.parts:
                continue
            offenders += gate_identities(relative, path.read_text(encoding="utf-8"))
    new_gates = sorted(set(offenders) - D10_GATE_CEILING)
    stale_entries = sorted(D10_GATE_CEILING - set(offenders))
    # Stable identities must not let another call reuse an existing entry.
    duplicate_gates = sorted(gate for gate, count in Counter(offenders).items() if count > 1)
    assert not new_gates and not stale_entries and not duplicate_gates, (
        f"direct gates outside the D10 ceiling (use a lane from tests/lanes.py): {new_gates}; "
        f"stale D10 ceiling entries (remove with the AT5 gate fix): {stale_entries}; "
        f"multiple gates sharing one D10 identity: {duplicate_gates}"
    )


@pytest.fixture
def d10_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A disposable scan root with one real frozen identity, never a live gate edit."""
    entry = (
        "tests/core/pack/test_identity.py",
        "test_digest_rejects_symlink_even_when_named_as_excluded_directory",
        "pytest.skip",
    )
    assert entry in FROZEN_D10_GATES
    site = tmp_path / entry[0]
    site.parent.mkdir(parents=True)
    site.write_text(f"import pytest\ndef {entry[1]}():\n    pytest.skip('existing gate')\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    monkeypatch.setitem(globals(), "D10_GATE_CEILING", frozenset({entry}))
    return site


def _plant_new_gate() -> tuple[str, str, str]:
    path = "tests/test_d10_planted_gate.py"
    (REPO / path).write_text("import pytest\ndef test_new():\n    pytest.skip('new gate')\n", encoding="utf-8")
    return path, "test_new", "pytest.skip"


def test_d10_rejects_new_gate_even_when_added_to_live_ceiling(d10_repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new_entry = _plant_new_gate()
    monkeypatch.setitem(globals(), "D10_GATE_CEILING", D10_GATE_CEILING | {new_entry})

    with pytest.raises(AssertionError, match="entries outside the frozen D10 set"):
        test_no_test_file_skips_by_hand()


def test_d10_rejects_swapping_a_gate_and_its_entry(d10_repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d10_repository.write_text("# The old gate was removed.\n", encoding="utf-8")
    new_entry = _plant_new_gate()
    monkeypatch.setitem(globals(), "D10_GATE_CEILING", frozenset({new_entry}))

    with pytest.raises(AssertionError, match="entries outside the frozen D10 set"):
        test_no_test_file_skips_by_hand()


def test_d10_ignores_comment_and_blank_line_shifts(d10_repository: Path) -> None:
    d10_repository.write_text("# Harmless comment.\n\n\n" + d10_repository.read_text(encoding="utf-8"), encoding="utf-8")

    test_no_test_file_skips_by_hand()


def test_d10_rejects_an_unlisted_new_gate(d10_repository: Path) -> None:
    _plant_new_gate()

    with pytest.raises(AssertionError, match="direct gates outside the D10 ceiling.*test_d10_planted_gate"):
        test_no_test_file_skips_by_hand()


def test_d10_rejects_a_removed_gate_left_in_the_ceiling(d10_repository: Path) -> None:
    d10_repository.write_text("# The gate was removed without retiring its entry.\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="stale D10 ceiling entries.*test_digest_rejects_symlink"):
        test_no_test_file_skips_by_hand()


def test_d10_allows_a_gate_and_its_entry_to_retire_together(d10_repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d10_repository.write_text("# AT5 removed this gate.\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "D10_GATE_CEILING", frozenset())

    test_no_test_file_skips_by_hand()


def test_d10_rejects_an_extra_gate_with_the_same_identity(d10_repository: Path) -> None:
    with d10_repository.open("a", encoding="utf-8") as stream:
        stream.write("    pytest.skip('another gate in the same function')\n")

    with pytest.raises(AssertionError, match="multiple gates sharing one D10 identity.*test_digest_rejects_symlink"):
        test_no_test_file_skips_by_hand()


@pytest.mark.parametrize(("source", "function", "api"), [
    ("import pytest\npytest.skip('x')\n", "<module>", "pytest.skip"),
    ("import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test(): pass\n", "test", "pytest.mark.skipif"),
    ("import pytest\nclass T:\n    def test(self):\n        pytest.skip('x')\n", "T.test", "pytest.skip"),
    ("import pytest\ndef outer():\n    def inner():\n        pytest.skip('x')\n", "outer.<locals>.inner", "pytest.skip"),
    ("import pytest\nasync def test():\n    pytest.skip('x')\n", "test", "pytest.skip"),
], ids=["module", "decorator", "method", "nested", "async"])
def test_gate_identity_uses_enclosing_function_qualname(source: str, function: str, api: str) -> None:
    assert gate_identities("tests/test_example.py", source) == [("tests/test_example.py", function, api)]
