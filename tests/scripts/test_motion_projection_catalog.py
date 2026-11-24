"""RN-5b-min catalog replacement preserves all non-motion descriptor bytes."""

import json

import pytest

from scripts.project_astrid_motion_catalog import replace_motion_rows


def test_replaces_only_motion_rows_and_retains_other_bytes():
    other = '{"packId":"rendering", "id":"fade", "kind":"transition", "duration":1.0}'
    old = '{"packId":"astrid_motion", "id":"am-type", "kind":"effect", "revision":"old"}'
    source = "prefix\nObject.freeze(\n[\n  " + other + ",\n  " + old + "\n]\n);\nsuffix\n"
    replacement = {"packId": "astrid_motion", "id": "am-type", "kind": "effect", "revision": "new"}
    result = replace_motion_rows(source, [replacement])
    assert other in result
    assert old not in result
    assert result.startswith("prefix\nObject.freeze(\n")
    assert result.endswith("\n);\nsuffix\n")
    rows = json.loads(result.split("Object.freeze(\n")[1].split("\n);\n")[0])
    assert rows[1] == replacement
    assert replace_motion_rows(result, [replacement]) == result


def test_rejects_missing_motion_rows():
    source = 'Object.freeze(\n[{"packId":"astrid_motion","kind":"effect","id":"old"}]\n);\n'
    with pytest.raises(ValueError, match="Unexpected"):
        replace_motion_rows(source, [{"packId": "astrid_motion", "kind": "effect", "id": "new"}])
