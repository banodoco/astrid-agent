"""Exact review reproductions for descriptor-based bundle confinement."""

import pytest

from astrid.packs.runpod import failure_bundle as bundle
from astrid.packs.runpod.failure_receipt import write_receipt


def test_review_10_intermediate_runtime_symlink(tmp_path):
    engine = tmp_path / 'engine'
    engine.mkdir()
    private = tmp_path / 'private'
    log = private / 'out/sessions/comfyui/comfy.log'
    log.parent.mkdir(parents=True)
    log.write_text('private-note')
    (engine / 'runtime').symlink_to(private, target_is_directory=True)
    assert bundle._confined(str(engine), [str(tmp_path)]) == str(engine)
    with pytest.raises(OSError):
        bundle.read_tail(str(engine / 'runtime/out/sessions/comfyui/comfy.log'))


def test_review_10_output_root_symlink(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    root = tmp_path / 'failures'
    root.symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError):
        bundle.collect({'name': 'failure', 'failures_root': str(root), 'roots': [str(tmp_path)]}, environ={})
    assert not list(private.iterdir())


def test_review_10_local_receipt_intermediate_symlink(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    root = tmp_path / 'failures'
    root.symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError):
        write_receipt({'identity': {'task_id': 'failure'}, 'files': []}, root=root)
    assert not list(private.iterdir())


def test_review_10_local_write_leaf_symlink(tmp_path):
    private = tmp_path / 'private-note'
    private.write_text('private-note')
    folder = tmp_path / 'bundle'
    folder.mkdir()
    (folder / 'error-tail.txt').symlink_to(private)
    with pytest.raises(OSError):
        bundle._write(str(folder), 'error-tail.txt', 'replacement')
    assert private.read_text() == 'private-note'
