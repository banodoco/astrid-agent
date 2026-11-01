from dataclasses import replace
import time

import pytest

from astrid.core.generation.engine_install_task import (
    prepare_install, recheck_install, recheck_install_under_custody,
)
from astrid.core.generation.engine_platforms import platform_adapter


def test_binding_rechecked_after_real_install_lock_creates_root(tmp_path):
    root = tmp_path / 'new' / 'engine'
    binding, _ = prepare_install(root=str(root))
    adapter = platform_adapter(binding.platform)
    with adapter.local_install_lock(str(root), deadline=time.monotonic() + 10):
        with pytest.raises(ValueError, match='binding changed'):
            recheck_install(binding)
        identity = recheck_install_under_custody(binding)
        assert recheck_install_under_custody(binding, root_identity=identity) == identity
        with pytest.raises(ValueError, match='binding changed'):
            recheck_install_under_custody(replace(binding, python='/tmp/forged'), root_identity=identity)


def test_custody_detects_replaced_root_and_original_anchor(tmp_path):
    anchor = tmp_path / 'anchor'
    anchor.mkdir()
    root = anchor / 'engine'
    binding, _ = prepare_install(root=str(root))
    adapter = platform_adapter(binding.platform)
    with adapter.local_install_lock(str(root), deadline=time.monotonic() + 10):
        identity = recheck_install_under_custody(binding)
        root.rename(anchor / 'old-engine')
        root.mkdir()
        with pytest.raises(ValueError, match='root changed'):
            recheck_install_under_custody(binding, root_identity=identity)
        anchor.rename(tmp_path / 'old-anchor')
        root.mkdir(parents=True)
        with pytest.raises(ValueError, match='anchor changed'):
            recheck_install_under_custody(binding, root_identity=identity)


def test_custody_rejects_unrelated_anchor(tmp_path):
    root = tmp_path / 'engine'
    binding, _ = prepare_install(root=str(root))
    root.mkdir()
    with pytest.raises(ValueError, match='outside the root ancestry'):
        recheck_install_under_custody(replace(binding, anchor='/unrelated'))
