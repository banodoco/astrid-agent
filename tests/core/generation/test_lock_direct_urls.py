"""Every direct-URL entry in a shipped engine lock carries exactly its URL's own hash.

Found live: a re-lock merged a registry requirement for setuptools into its direct-URL
entry, adding a second hash. `uv pip sync --require-hashes` on the pod then refused
the environment with "Conflicting archive URL hashes" before anything ran.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LOCKS = sorted(Path(__file__).resolve().parents[3].glob("astrid/packs/*/requirements.*.lock"))


def _entries(text: str):
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        lines = block.splitlines()
        if lines and " @ " in lines[0]:
            yield lines[0], re.findall(r"--hash=sha256:([0-9a-f]{64})", block)


@pytest.mark.parametrize("lock", LOCKS, ids=lambda path: path.name)
def test_direct_url_entries_carry_only_their_own_hash(lock):
    assert LOCKS, "no shipped engine lock found"
    for first, hashes in _entries(lock.read_text(encoding="utf-8")):
        fragment = re.search(r"#sha256=([0-9a-f]{64})", first)
        assert len(hashes) == 1, f"{first.split(' @ ')[0]} has {len(hashes)} hashes for one archive URL"
        if fragment:
            assert hashes == [fragment.group(1)], f"{first.split(' @ ')[0]} hash differs from its URL fragment"
