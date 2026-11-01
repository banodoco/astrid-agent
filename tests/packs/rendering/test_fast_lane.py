"""Unit-level fast-lane timing checks (no Chrome, no Node)."""
import os
import time

from astrid.core import gateway


def test_gateway_main_stamps_the_cli_start_for_the_check_timing(monkeypatch):
    monkeypatch.delenv("ASTRID_CLI_START_MONO", raising=False)
    before = time.monotonic()
    assert gateway.main(["--version"]) == 0
    stamp = float(os.environ.pop("ASTRID_CLI_START_MONO"))
    assert stamp <= time.monotonic()
    # The first call stamps the gateway import; it is never later than this test's start by a wide margin.
    assert stamp <= before + 1.0
