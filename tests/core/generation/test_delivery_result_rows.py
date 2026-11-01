"""Manifest-only delivery fallback delivers result rows only, never other roles."""

from astrid.core.generation.delivery import _result_rows


def test_mixed_roles_deliver_only_result_rows():
    outputs = [
        {"object_id": "obj-result", "role": "result"},
        {"object_id": "obj-log", "role": "log"},
        {"object_id": "obj-debug", "role": "debug"},
        {"object_id": "obj-default"},
    ]
    rows = _result_rows(outputs)
    assert [row["object_id"] for row in rows] == ["obj-result", "obj-default"]


def test_no_result_rows_yields_nothing_so_delivery_fails_closed():
    outputs = [{"object_id": "obj-log", "role": "log"}]
    assert _result_rows(outputs) == []


def test_non_mapping_entries_are_ignored():
    assert _result_rows(["junk", {"object_id": "obj", "role": "result"}]) == [
        {"object_id": "obj", "role": "result"}
    ]
