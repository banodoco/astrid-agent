"""The Windows target comparison must reject ID drift even at equal counts."""
import pytest

from tests.ls5_windows_baseline import require_parity


def outcomes():
    return {f"test_target_{index}": "ERROR" if index < 40 else "PASS" for index in range(52)}


def test_matching_windows_target_errors_are_compared_without_becoming_a_packet_failure():
    base = outcomes()
    require_parity(base, dict(base))


@pytest.mark.parametrize("drift", ["new-error", "swapped-error", "renamed-case", "missing-case", "skipped-case"])
def test_windows_target_comparison_rejects_id_drift(drift):
    base = outcomes()
    candidate = dict(base)
    if drift == "new-error":
        candidate["test_target_51"] = "ERROR"
    elif drift == "swapped-error":
        candidate["test_target_0"], candidate["test_target_51"] = "PASS", "ERROR"
    elif drift == "renamed-case":
        candidate["test_new_target"] = candidate.pop("test_target_0")
    elif drift == "missing-case":
        candidate.pop("test_target_0")
    else:
        candidate["test_target_0"] = "SKIPPED"
    with pytest.raises(AssertionError):
        require_parity(base, candidate)
