"""Report shared source preparation without importing the integration helpers."""
import sys

import pytest

_READER_RESETS = []


@pytest.fixture(autouse=True)
def _generation_case_reader_cache(request):
    if request.path.name not in {
        "test_runpod_slot_path_cpu.py", "test_dry_run_equals_live_run.py",
        "test_runpod_dry_live_seam.py",
    }:
        return
    reader = sys.modules.get("vibecomfy.porting.object_info.consume")
    if reader is not None:
        # cause 004bc946 (VibeComfy): every scalar lookup re-hashes all loaded
        # packs. Keep independent cases from accumulating each other's packs;
        # the public reset retains all on-disk content/provenance checks.
        _READER_RESETS.append(len(reader._pack_cache))
        reader.reset_cache()


def pytest_terminal_summary(terminalreporter):
    support = sys.modules.get("tests.integrations._pinned_source")
    if support is not None:
        terminalreporter.write_line(f"pinned-source session: {support.SOURCE_COUNTS}")
    if _READER_RESETS:
        terminalreporter.write_line(f"object-info case resets: {_READER_RESETS}")
