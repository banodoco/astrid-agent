"""RA-0b witnesses for the seal and the shared test-only forging helper."""

from __future__ import annotations

import stat

import pytest

from astrid.installer.core import install_stamp
from tests.installer.schema_seal_helpers import (
    _forge_installed_file,
    _temporary_schema_write_access,
)
from tests.test_installer_installed_schema import (
    SCHEMA,
    release_wheel,  # noqa: F401 - pytest fixture dependency
    staged_installer,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.linux_native


@pytest.mark.non_root
def test_staged_schema_denies_owner_write_and_unlink(staged_installer):  # noqa: F811
    state = staged_installer
    state.backend.stage(state.manifest, state.root, state.data)
    path = state.product_root / SCHEMA
    before = path.read_bytes()
    assert not path.stat().st_mode & 0o222
    assert not path.parent.stat().st_mode & 0o222
    with pytest.raises(PermissionError):
        path.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        path.unlink()
    assert path.read_bytes() == before
    stamp, reason = install_stamp(state.product_root / "astrid")
    assert stamp and reason is None


@pytest.mark.parametrize("damage", ["missing", "symlink", "mismatch"])
def test_staged_schema_damage_refused_after_permissions_restored(
    staged_installer, damage,  # noqa: F811
):
    state = staged_installer
    version = state.backend.stage(state.manifest, state.root, state.data)
    path = state.product_root / SCHEMA
    package = state.product_root / "astrid"
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert not parent_mode & 0o222
    assert not file_mode & 0o222
    stamp, reason = install_stamp(package)
    assert stamp and reason is None

    target = state.outside / "identical-schema-for-seal-witness.json"
    target.write_bytes(path.read_bytes())
    target_mode = target.stat().st_mode
    _forge_installed_file(path, damage, target)

    # Restoration occurs before testing the stamp. The mismatch case must
    # refuse on bytes, not on a still-writable file or directory.
    assert stat.S_IMODE(path.parent.stat().st_mode) == parent_mode
    if damage == "mismatch":
        assert stat.S_IMODE(path.stat().st_mode) == file_mode
    elif damage == "symlink":
        assert path.is_symlink()
        assert target.stat().st_mode == target_mode
    else:
        assert not path.exists()
    relative = path.relative_to(version.root).as_posix()
    assert install_stamp(package) == (None, f"shipped_file_modified:{relative}")


def test_schema_permission_scope_restores_seal_on_exception(staged_installer):  # noqa: F811
    state = staged_installer
    version = state.backend.stage(state.manifest, state.root, state.data)
    path = state.product_root / SCHEMA
    package = state.product_root / "astrid"
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode)
    before = install_stamp(package)
    assert before[0] and before[1] is None

    with pytest.raises(RuntimeError, match="forging interrupted"):
        with _temporary_schema_write_access(path):
            assert path.parent.stat().st_mode & stat.S_IWUSR
            assert path.stat().st_mode & stat.S_IWUSR
            raise RuntimeError("forging interrupted")

    assert stat.S_IMODE(path.parent.stat().st_mode) == parent_mode
    assert stat.S_IMODE(path.stat().st_mode) == file_mode
    relative = path.relative_to(version.root).as_posix()
    assert install_stamp(package) == (None, f"shipped_file_modified:{relative}")
    assert install_stamp(package, verify=True) == before
