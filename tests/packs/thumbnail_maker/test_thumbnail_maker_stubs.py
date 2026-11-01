"""Smoke tests: thumbnail_maker stub executors raise NotImplementedError."""

import argparse
import pytest

from astrid.packs.video_editing.actions.thumbnail_maker.run import (
    _exec_discover_video_evidence,
    _exec_build_reference_pack,
    _exec_generate_thumbnails,
)


def _dummy_args():
    return argparse.Namespace()


def test_discover_video_evidence_raises():
    with pytest.raises(NotImplementedError, match="thumbnail_maker.discover_video_evidence"):
        _exec_discover_video_evidence(_dummy_args())


def test_build_reference_pack_raises():
    with pytest.raises(NotImplementedError, match="thumbnail_maker.build_reference_pack"):
        _exec_build_reference_pack(_dummy_args())


def test_generate_thumbnails_raises():
    with pytest.raises(NotImplementedError, match="thumbnail_maker.generate_thumbnails"):
        _exec_generate_thumbnails(_dummy_args())
