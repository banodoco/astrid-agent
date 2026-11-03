import os

import pytest

from scripts import m4_6_linux_principal_discriminator as seam


def test_one_use_rejects_foreign_and_replay():
    ledger = seam.OneUseRequest('request-1')
    with pytest.raises(seam.Refusal, match='foreign'):
        ledger.consume('foreign')
    with pytest.raises(seam.Refusal, match='replayed'):
        ledger.consume('request-1')
    ledger = seam.OneUseRequest('request-1')
    assert ledger.consume('request-1') == 'request-1'
    with pytest.raises(seam.Refusal, match='replayed'):
        ledger.consume('request-1')


def test_child_rejects_real_identity_mismatch():
    current = seam.identity(os.getpid())
    seam.verify_child(os.getpid(), os.getuid(), current['exe'],
                      [os.fsdecode(a) for a in current['argv']], current['cwd'], current['birth'])
    with pytest.raises(seam.Refusal, match='drift'):
        seam.verify_child(os.getpid(), os.getuid(), current['exe'],
                          [os.fsdecode(a) for a in current['argv']] + ['foreign'], current['cwd'], current['birth'])
    with pytest.raises(seam.Refusal, match='drift'):
        seam.verify_child(os.getpid(), os.getuid(), current['exe'],
                          [os.fsdecode(a) for a in current['argv']], current['cwd'], current['birth'] + '1')


def test_uid_zero_refused_before_publication(tmp_path):
    before = set(tmp_path.iterdir())
    with pytest.raises(seam.Refusal, match='non-root'):
        seam.run(0, tmp_path)
    assert set(tmp_path.iterdir()) == before
