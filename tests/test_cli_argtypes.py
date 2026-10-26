from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from astrid.core.cli.argtypes import (
    execution_request,
    input_file,
    json_array,
    json_object,
    output_file,
)
from astrid.packs.timeline.cli import _json_object_or_path


def test_json_object_and_array_types_keep_their_shapes_and_errors() -> None:
    assert json_object('{"title": "A"}') == {"title": "A"}
    assert json_array('["a", 2]') == ["a", 2]
    with pytest.raises(argparse.ArgumentTypeError, match="invalid JSON object: Expecting value"):
        json_object("not json")
    with pytest.raises(argparse.ArgumentTypeError, match="must be a JSON object"):
        json_object("[]")
    with pytest.raises(argparse.ArgumentTypeError, match="invalid JSON array: Expecting value"):
        json_array("not json")
    with pytest.raises(argparse.ArgumentTypeError, match="must be a JSON array"):
        json_array("{}")


def test_execution_request_type_normalizes_and_keeps_its_errors() -> None:
    raw = {"target": {"kind": "profile", "id": "h3-cu130"}}
    normalized = execution_request(json.dumps(raw))
    assert normalized == raw
    assert normalized is not raw
    with pytest.raises(argparse.ArgumentTypeError, match="invalid execution request: Expecting value"):
        execution_request("not json")
    with pytest.raises(argparse.ArgumentTypeError, match="execution_request.target is required"):
        execution_request("{}")


def test_file_types_return_paths_without_changing_path_semantics(tmp_path: Path) -> None:
    target = str(tmp_path / "notes.md")
    assert input_file(target) == Path(target)
    assert output_file(target) == Path(target)


def test_timeline_json_object_or_path_stays_dual_for_the_cli(tmp_path: Path) -> None:
    source = tmp_path / "rules.json"
    source.write_text('{"params": {"motion": 0.4}}')
    assert _json_object_or_path('{"params": {"motion": 0.4}}') == {"params": {"motion": 0.4}}
    assert _json_object_or_path(str(source)) == str(source)
