"""P4's provenance-backed early cases; execution waits for G2/G3/R2."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline_sheet import parse_sheet
from tests.editing_conformance.fixture_loader import load_fixture
from tests.sdk.test_timeline_sync import initial_bundle

ROOT = Path(__file__).parents[2]
CASES = ROOT / "tests/sdk/fixtures/p4_commit_cases/cases.json"


def _load() -> dict[str, Any]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def _source(data: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(source for source in data["sources"] if source["id"] == source_id)


def _source_function(source: dict[str, Any]) -> ast.FunctionDef:
    path = ROOT / source["path"]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == source["symbol"]
    ]
    assert len(functions) == 1, source["symbol"]
    function = functions[0]
    assert isinstance(function, ast.FunctionDef), source["symbol"]
    return function


def _assert_single_edit_source(source: dict[str, Any]) -> None:
    function = _source_function(source)
    operation = source["edit_operation"]
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]

    if source["kind"] == "python":
        edits = [
            call
            for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == operation
        ]
    else:
        edits = [
            call
            for call in calls
            if isinstance(call.func, ast.Name)
            and call.func.id == "run"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "edit"
            and any(
                isinstance(argument, ast.Constant) and argument.value == "--on"
                for argument in call.args
            )
        ]

    assert len(edits) == 1, source["symbol"]


def test_p4_sources_are_real_and_parseable() -> None:
    data = _load()
    for source in data["sources"]:
        path = ROOT / source["path"]
        assert path.is_file(), source["path"]

    for source_id in ("cli", "python"):
        _assert_single_edit_source(_source(data, source_id))

    sheet = parse_sheet((ROOT / _source(data, "sheet")["path"]).read_text(encoding="utf-8"))
    assert sheet["version"] == "0ca00d3ed76f"
    assert [cut["id"] for cut in sheet["cuts"]] == ["c1", "c2"]
    assert [line["text"] for line in sheet["lines"].values()] == ["It went viral.", "Live now."]
    assert sheet["sound"][0]["asset"] == "Q"

    bundle = initial_bundle()
    clips = {clip["id"]: clip for clip in bundle["parent"]["clips"]}
    assert clips["a"]["at"] == 0
    assert clips["b"]["at"] == 0

    fixture_path = ROOT / _source(data, "document")["path"]
    document = load_fixture(fixture_path)["input"]
    assert document["project_id"] == "project-1"
    assert document["timeline_id"] == "film"
    assert document_from_bundle(bundle_from_document(document)) == document


def test_p4_case_shape_covers_front_doors_and_pending_execution() -> None:
    data = _load()
    assert data["execution"]["status"] == "pending"
    assert {source["kind"] for source in data["sources"]} >= {
        "cli",
        "python",
        "sheet",
        "editing-conformance",
    }

    single = next(case for case in data["cases"] if case["id"] == "single-real-edit")
    assert set(single["sources"]) == {"cli", "python", "sheet"}
    assert single["field_deltas"] == [
        {
            "ref": "parent",
            "path": ["clips", "id:clip-a", "at"],
            "before": {"state": "present", "value": 0},
            "after": 5,
        }
    ]
    assert single["execution"] == "pending-contract-dependent"

    notes = next(case for case in data["cases"] if case["id"] == "ten-notes-one-commit")
    assert notes["commit"]["command_count"] == notes["commit"]["step_count"] == 10
    assert [note["ordinal"] for note in notes["notes"]] == list(range(10))
    assert [note["id"] for note in notes["notes"]] == [f"note-{index:02d}" for index in range(1, 11)]
    assert notes["patch_template"]["before"] == {"state": "absent"}
    assert len(notes["field_deltas"]) == 10
    assert [delta["path"][-1] for delta in notes["field_deltas"]] == [
        f"note-{index:02d}" for index in range(1, 11)
    ]
    assert [delta["after"] for delta in notes["field_deltas"]] == [
        note["text"] for note in notes["notes"]
    ]
    assert notes["independent_step_undo"]["targets"] == "each note-01 through note-10 independently"
    assert notes["conflict"]["expected"] == "conflict"
    assert notes["rerun"]["max_attempts"] == 3
    assert notes["rerun"]["unknown_outcome_is_not_a_rerun_trigger"] is True
