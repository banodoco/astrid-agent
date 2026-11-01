"""The two capability-matrix copies must stay byte-identical."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_capability_matrix_copies_are_identical():
    source = REPO_ROOT / "config" / "astrid-beta-capabilities.json"
    packaged = REPO_ROOT / "astrid" / "resources" / "astrid-beta-capabilities.json"
    assert source.read_bytes() == packaged.read_bytes(), (
        "config/ and astrid/resources/ copies of astrid-beta-capabilities.json have drifted; "
        "update both together"
    )
