from scripts.reshape.installed_artifact import _failure_detail


def test_a_failed_lane_names_its_status_and_carries_stderr():
    stderr = "Traceback (most recent call last):\n  File 'cli.py', line 3\nModuleNotFoundError: No module named 'x'\n"
    detail = _failure_detail("lane exited with status 1", stderr)
    assert detail.startswith("lane exited with status 1\n--- lane stderr")
    assert "ModuleNotFoundError: No module named 'x'" in detail


def test_a_silent_failure_keeps_only_the_status():
    assert _failure_detail("lane exited with status 1", "   \n") == "lane exited with status 1"


def test_only_the_tail_of_a_long_stderr_is_kept():
    stderr = "\n".join(f"line {i}" for i in range(200))
    detail = _failure_detail("lane exited with status 1", stderr, lines=5)
    assert "line 199" in detail and "line 194" not in detail
