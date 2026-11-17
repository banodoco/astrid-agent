"""No test leaves itself out by hand: a skip is a lane marker from the registry in tests/lanes.py.

The runtime guard (tests/test_skip_guard.py) fails a run on any skip that no lane declares. This is its static
half: it finds the skip and xfail calls that would only fire on some other machine, so they cannot wait there to
hide a test. tests/lanes.py is the one place that may build a skip.
"""

from __future__ import annotations

import ast
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
    # Owner-approved Plan 39 D10 ceiling, captured at 1148df0e. This list
    # may only shrink: AT5 removes each gate and its entry together.
    ceiling = {
        "tests/core/execution/test_host_cleanup_ack_barrier.py:16 pytest.mark.skipif",
        "tests/core/generation/test_model_verification_record.py:590 pytest.skip",
        "tests/core/generation/test_release_python.py:58 pytest.mark.skipif",
        "tests/core/generation/test_release_python.py:498 pytest.mark.skipif",
        "tests/core/pack/test_git_environment.py:289 pytest.skip",
        "tests/core/pack/test_git_environment.py:369 pytest.skip",
        "tests/core/pack/test_identity.py:56 pytest.skip",
        "tests/scripts/test_pod_bootstrap.py:115 pytest.mark.skipif",
        "tests/scripts/test_pod_session.py:41 pytest.skip",
        "tests/test_installed_wheel_smoke.py:25 pytest.skip",
        "tests/test_installer_pinokio_activation.py:175 pytest.mark.skipif",
        "tests/test_installer_r0_entry.py:618 pytest.skip",
        "tests/test_installer_r0_stop.py:237 pytest.skip",
        "tests/test_journey_diagnostic_checkpoint.py:242 pytest.skip",
        "tests/test_journey_diagnostic_checkpoint.py:273 pytest.skip",
        "tests/test_journey_windows_acl_capture.py:28 pytest.skip",
    }
    offenders = []
    for root in (REPO / "tests", *sorted((REPO / "tools").glob("*/tests"))):
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if relative == REGISTRY or ".venv" in path.parts or "node_modules" in path.parts:
                continue
            offenders += [f"{relative}:{line} {name}" for line, name in hidden_gates(path.read_text(encoding="utf-8"))]
    new_gates = sorted(set(offenders) - ceiling)
    stale_entries = sorted(ceiling - set(offenders))
    assert not new_gates and not stale_entries, (
        f"direct gates outside the D10 ceiling (use a lane from tests/lanes.py): {new_gates}; "
        f"stale D10 ceiling entries (remove with the AT5 gate fix): {stale_entries}"
    )
