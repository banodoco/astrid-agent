"""The installer prepares, but never bypasses, Timeline input custody."""

import shutil
import sys
from pathlib import Path

import pytest

from astrid.core.rendering import _local_schema
from astrid.installer import backend


@pytest.fixture
def product_root(tmp_path):
    root = tmp_path / "site-packages"
    source = Path(__file__).resolve().parents[1]
    for relative in (_local_schema.SCHEMA_RELATIVE_PATH, Path("runtime.lock.json"),
                     *_local_schema.READER_INPUTS):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    return root


def test_windows_prepares_every_checked_input_and_parent(product_root, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backend.platform, "owner_only", calls.append)
    backend.Backend._secure_installed_timeline_inputs(product_root)
    expected = {product_root}
    for relative in (_local_schema.SCHEMA_RELATIVE_PATH, Path("runtime.lock.json"),
                     *_local_schema.READER_INPUTS):
        path = product_root / relative
        while path != product_root:
            expected.add(path)
            path = path.parent
    assert set(calls) == expected
    assert len(calls) == len(expected)
    assert calls == sorted(calls, key=lambda path: (len(path.parts), str(path)))


def test_windows_acl_failure_refuses_admission(product_root, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def refuse(_path):
        raise PermissionError("foreign owner or unprotectable DACL")

    monkeypatch.setattr(backend.platform, "owner_only", refuse)
    with pytest.raises(PermissionError, match="foreign owner"):
        backend.Backend._secure_installed_timeline_inputs(product_root)


@pytest.mark.linux_native
def test_windows_rejects_aliased_input_before_changing_acl(product_root, tmp_path, monkeypatch):
    schema = product_root / _local_schema.SCHEMA_RELATIVE_PATH
    outside = tmp_path / "outside.json"
    shutil.copyfile(schema, outside)
    schema.unlink()
    schema.symlink_to(outside)
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(backend.platform, "owner_only", calls.append)
    with pytest.raises(ValueError, match="aliased"):
        backend.Backend._secure_installed_timeline_inputs(product_root)
    assert calls == []


@pytest.mark.linux_native
def test_posix_does_not_repair_unsafe_permissions_or_foreign_owner(product_root, monkeypatch):
    schema = product_root / _local_schema.SCHEMA_RELATIVE_PATH
    schema.chmod(0o666)
    backend.Backend._secure_installed_timeline_inputs(product_root)
    with pytest.raises(ValueError, match="owner/mode"):
        _local_schema.verified_schema_path(product_root, str(schema))
    schema.chmod(0o600)
    monkeypatch.setattr(_local_schema, "owned_by_current_user", lambda *_args, **_kwargs: False)
    with pytest.raises(ValueError, match="owner/mode"):
        _local_schema.verified_schema_path(product_root, str(schema))


@pytest.mark.windows_native
def test_windows_native_acls_satisfy_schema_reader(product_root):
    backend.Backend._secure_installed_timeline_inputs(product_root)
    schema = product_root / _local_schema.SCHEMA_RELATIVE_PATH
    assert _local_schema.verified_schema_path(product_root, str(schema)) == schema
