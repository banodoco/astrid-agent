"""Plan 55's exact source tuple with pytest-owned short-path lifetime.

Same checkout identity, Runtime pin, cleanliness/archive consumers as the
retained bootstrap fixture. No permission repair or manual deletion of Git
object files. This private fixture avoids changing unrelated RunPod tests.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import pytest

_TUPLE_SOURCES = pytest.StashKey[Path]()

@pytest.fixture(scope="session")
def tuple_sources(tmp_path_factory, request):
    """One clean shared-object source tuple for the real remote bootstrap gate."""
    # Imported fixture aliases share one session-owned, immutable source tuple.
    # Each consumer still runs the real source pin/cleanliness/archive gates.
    if _TUPLE_SOURCES in request.session.stash:
        yield request.session.stash[_TUPLE_SOURCES]
        return
    root = tmp_path_factory.mktemp("t")
    selected = {
        "astrid-runtime": Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]),
        "runpod-lifecycle": Path(os.environ["ASTRID_TEST_LIFECYCLE_CHECKOUT"]),
        "astrid-agent": Path(__file__).resolve().parents[2],
        "VibeComfy": Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"]),
    }
    for name, source in selected.items():
        destination = root / "src" / name
        subprocess.run(["git", "clone", "--quiet", "--shared", str(source), str(destination)], check=True)
        expected = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if name == "astrid-runtime":
            # The worker bootstrap is pinned by the Agent's lock, independently
            # of the Runtime used to serve this controller-side test process.
            expected = json.loads((selected["astrid-agent"] / "runtime.lock.json").read_text())["source_commit"]
            present = subprocess.run(["git", "-C", str(destination), "cat-file", "-e", expected],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if present.returncode:
                subprocess.run(["git", "-C", str(destination), "fetch", "--quiet", "--depth=1",
                                "https://github.com/banodoco/astrid-runtime", expected], check=True)
            subprocess.run(["git", "-C", str(destination), "checkout", "--quiet", "--detach", expected], check=True)
        assert subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip() == expected
    request.session.stash[_TUPLE_SOURCES] = root
    yield root
    # tmp_path_factory owns this session directory and its cleanup. Deleting
    # shared Git object files here masks the original failure on Windows.

