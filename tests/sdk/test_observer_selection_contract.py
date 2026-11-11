"""Focused, synthetic proof of the retained observer wire boundary."""
import json
import struct

import pytest

from astrid.sdk import observer_selection as selection


def record(tmp_path):
    return {
        "version": 1, "operation": "pack-host-observe-v1", "selection_id": "a" * 32,
        "endpoint": "http://127.0.0.1:1234", "instance_id": "instance", "session_id": "session",
        "epoch": 2, "data_root": str(tmp_path), "mode": "installed",
        "interpreter": "/venv/bin/python", "import_roots": [],
    }


def parse(value, tmp_path):
    return selection.parse_selection(value, data_root=tmp_path, endpoint="http://127.0.0.1:1234",
                                     instance_id="instance", session_id="session", epoch=2)


def test_selected_interpreter_is_retained_without_resolving_venv_symlink(tmp_path):
    selected = record(tmp_path)
    assert parse(json.dumps(selected).encode(), tmp_path)["interpreter"] == "/venv/bin/python"


@pytest.mark.parametrize("change", [
    {"operation": "other"}, {"selection_id": "a" * 31}, {"epoch": True},
    {"epoch": 3}, {"instance_id": "foreign"}, {"session_id": "foreign"},
    {"endpoint": "http://127.0.0.1:9999"}, {"data_root": "/foreign"},
    {"interpreter": "python"}, {"mode": "other"}, {"import_roots": ["/tmp"]},
    {"unrecognized": "value"}, {"session_id": "x\u0000y"},
])
def test_selection_rejects_unbound_or_unrecognized_values(tmp_path, change):
    selected = record(tmp_path)
    selected.update(change)
    with pytest.raises(ValueError):
        parse(json.dumps(selected).encode(), tmp_path)


def test_selection_rejects_duplicate_keys_and_oversize(tmp_path):
    data = json.dumps(record(tmp_path)).encode()
    with pytest.raises(ValueError):
        parse(data[:-1] + b', "version": 1}', tmp_path)
    with pytest.raises(ValueError):
        parse(b" " * 16385, tmp_path)


def test_frame_is_exactly_one_length_delimited_json():
    value = {"version": 1}
    payload = json.dumps(value).encode()
    assert selection.decode_frame(struct.pack(">I", len(payload)) + payload) == value
    for raw in (b"", struct.pack(">I", len(payload) + 1) + payload,
                struct.pack(">I", len(payload)) + payload + b"x",
                struct.pack(">I", 65533) + b"x" * 65533,
                b'\x00\x00\x00\x13{"a": 1, "a": 2}'):
        with pytest.raises(ValueError):
            selection.decode_frame(raw)
