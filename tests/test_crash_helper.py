"""The portable abrupt exit never enters exception cleanup."""
import os
import sys

from tests.helpers.crash import EXIT_CODE, run_owned


def test_portable_crash_does_not_unwind_finally(tmp_path):
    marker = tmp_path / "unwound"
    source = ("from pathlib import Path; from tests.helpers.crash import crash\n"
              "try:\n crash()\nfinally:\n Path(%r).write_text('unwound')\n" % str(marker))
    result = run_owned([sys.executable, "-c", source], timeout=10,
                       env={**os.environ, "ASTRID_TEST_CRASH_METHOD": "exit"})
    assert result.returncode == EXIT_CODE
    assert not marker.exists()
