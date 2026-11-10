"""Pod task storage lives on the container disk, never on the /workspace mount.

The generic host's free-space guard measures the device under the attempt root and the output root. On an
ephemeral pod /workspace is a small mount, so those roots must derive from the container-disk POD_ROOT.
"""
from pathlib import PurePosixPath

from astrid.packs.runpod import remote_placement as rp
from astrid.packs.runpod.pod_bootstrap import SCRIPT

CONTAINER_DISK_ROOT = PurePosixPath("/root/astrid")
WORKSPACE = PurePosixPath("/workspace")


def _roots():
    output_root = PurePosixPath(rp.RUNS_ROOT) / "run-1" / "out"
    return {
        "pod_root": PurePosixPath(rp.POD_ROOT),
        "engine_base": PurePosixPath(rp.ENGINE_BASE),
        "engine_root": PurePosixPath(rp.ENGINE_ROOT),
        "bootstrap_root": PurePosixPath(rp.BOOTSTRAP_ROOT),
        "runs_root": PurePosixPath(rp.RUNS_ROOT),
        "output_root": output_root,
        # worker_owner / host_bootstrap require attempt_base == output_root / "attempts"
        "attempt_base": output_root / "attempts",
    }


def test_pod_attempt_and_output_roots_are_not_under_workspace():
    for name, path in _roots().items():
        assert not path.is_relative_to(WORKSPACE), name
        assert path.is_relative_to(CONTAINER_DISK_ROOT), name


def test_pod_bootstrap_defaults_are_not_under_workspace():
    assert "/workspace" not in SCRIPT
    assert "${ASTRID_BOOTSTRAP_ROOT:-/root/astrid/bootstrap}" in SCRIPT
    assert "${ASTRID_REALM_ROOT:-/root/astrid/realm}" in SCRIPT


def test_engine_custody_root_is_unchanged():
    assert rp.ENGINE_BASE == "/root/astrid/engines"
    assert rp.ENGINE_ROOT == "/root/astrid/engines/comfyui"
