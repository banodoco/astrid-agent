"""Phase-1 output cardinality: exactly one declared sink, counted as VibeComfy counts them."""

import json

from astrid.sdk.generation import _declared_output_count


def _companion(tmp_path, document):
    path = tmp_path / "workflow.vibe.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_single_declared_sink_counts_one(tmp_path):
    path = _companion(tmp_path, {"outputs": [{"node_id": "9", "output_type": "SaveImage"}]})
    assert _declared_output_count(path) == 1


def test_two_declared_sinks_count_two(tmp_path):
    path = _companion(tmp_path, {"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": "10", "output_type": "SaveVideo"},
    ]})
    assert _declared_output_count(path) == 2


def test_mixed_role_entries_without_node_id_do_not_count(tmp_path):
    path = _companion(tmp_path, {"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": None, "output_type": "PreviewImage"},
    ]})
    assert _declared_output_count(path) == 1


def test_no_declared_outputs_counts_zero(tmp_path):
    path = _companion(tmp_path, {"outputs": []})
    assert _declared_output_count(path) == 0


def test_undeterminable_companion_returns_none(tmp_path):
    assert _declared_output_count(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert _declared_output_count(bad) is None
    assert _declared_output_count(_companion(tmp_path, {"nodes": {}})) is None
