from pathlib import Path
import os
import subprocess
import sys

import pytest

from tests.r0_reality.lifetime import OwnerBusy, OwnerLock, purge_after_unlock, session_worker


def test_owner_is_exclusive_until_close_and_reacquirable(tmp_path):
    path = tmp_path / "owner.lock"
    first = OwnerLock(path).acquire()
    try:
        with pytest.raises(OwnerBusy):
            OwnerLock(path).acquire()
        assert path.exists()  # presence alone is not ownership
    finally:
        first.close()
    with OwnerLock(path):
        pass


def test_owner_conflicts_across_processes(tmp_path):
    path = tmp_path / "owner.lock"
    code = ("from pathlib import Path; from tests.r0_reality.lifetime import OwnerLock, OwnerBusy; "
            "import sys\ntry: OwnerLock(Path(sys.argv[1])).acquire()\n"
            "except OwnerBusy: sys.exit(17)\nsys.exit(0)")
    with OwnerLock(path):
        result = subprocess.run([sys.executable, "-c", code, str(path)], check=False)
        assert result.returncode == 17
    assert subprocess.run([sys.executable, "-c", code, str(path)], check=False).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX session identity; Windows Job behavior is not modeled")
def test_worker_is_in_a_separate_session():
    with session_worker() as worker:
        assert os.getsid(worker.pid) == worker.pid
        assert os.getsid(worker.pid) != os.getsid(0)
        assert worker.poll() is None


@pytest.mark.skipif(os.name == "nt", reason="Windows file deletion sharing differs")
def test_purge_can_remove_a_new_owner_after_unlock(tmp_path):
    root = tmp_path / "realm"
    root.mkdir()
    new_owner = OwnerLock(root / "owner.lock")
    try:
        purge_after_unlock(root, after_unlock=new_owner.acquire)
        assert not root.exists()
        assert new_owner._file is not None
    finally:
        new_owner.close()
