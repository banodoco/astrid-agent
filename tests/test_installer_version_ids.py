"""Version directory identifiers stay short without owning source identities."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from astrid.installer import core


def test_new_version_ids_are_12_hex_and_each_retry_gets_a_fresh_directory(tmp_path, monkeypatch):
    manifest = {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}}
    original = json.loads(json.dumps(manifest))
    values = iter(("0123456789abcdef" * 2, "01234567fedcba98" * 2))
    monkeypatch.setattr(core.uuid, "uuid4", lambda: SimpleNamespace(hex=next(values)))
    versions = tmp_path / "program" / "versions"

    ids = [core.new_version_id(manifest) for _ in range(2)]
    for value in ids:
        assert re.fullmatch(r"[0-9a-f]{12}", value)
        (versions / value).mkdir(parents=True)

    assert ids == ["0123456789ab", "01234567fedc"]
    assert ids[0] != ids[1]
    assert {path.name for path in versions.iterdir()} == set(ids)
    assert manifest == original


@pytest.mark.parametrize("identifier", [
    "3204bb00-6d53a190-46310e10",
    "v1",
])
def test_historical_version_ids_still_validate_and_load_without_parsing_source_hashes(tmp_path, identifier):
    program = tmp_path / "program"
    folder = program / "versions" / identifier
    folder.mkdir(parents=True)
    version = core.Version(folder, folder / "py" / "python", folder / "profile.json", {})
    core.write_json(folder / "version.json", version.document())
    manifest = {"agent": {"sha": "c" * 40}, "runtime": {"sha": "d" * 40}}
    core.write_json(folder / "manifest.json", manifest)
    (program / "current").write_text(identifier + "\n", encoding="utf-8")

    assert core.version_id(identifier) == identifier
    loaded = core.current(program)

    assert loaded is not None
    assert loaded.root == folder
    assert core.read_json(folder / "manifest.json") == manifest
    assert core.read_json(folder / "version.json") == version.document()
