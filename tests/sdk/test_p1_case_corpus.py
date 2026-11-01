"""Data-driven H3 Agent feasibility corpus.

The fixtures are copied from existing conformance, scenario, SDK, and sample
project inputs.  This test intentionally exercises the current semantic command
and SDK checkout paths only; the future ``doc.patch`` provider is not present on
this branch, so its H3 execution remains pending rather than being simulated.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from astrid.core.timeline import load_timeline, save_timeline
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.rebase import replay

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).resolve().parent / "fixtures" / "p1_feasibility"
MANIFEST = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in MANIFEST["cases"]}


def _fixture(case_id: str) -> dict[str, Any]:
    return json.loads((CORPUS / CASES[case_id]["fixture"]).read_text(encoding="utf-8"))


def _selected(data: dict[str, Any], selector: str) -> Any:
    if selector in data:
        return data[selector]
    if selector == "commands[0]":
        return data["commands"][0]
    if selector == "commands":
        return data["commands"]
    if selector == "command":
        return data["command"]
    raise AssertionError(f"unsupported corpus selector: {selector}")


def _canonical_bundle(case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    case = CASES[case_id]
    fixture = _fixture(case_id)
    document = _selected(fixture, case["document"])
    if case["document"] == "input":
        bundle = bundle_from_document(document)
    else:
        bundle = copy.deepcopy(document)
    return bundle, fixture


def _qualified_python_nodes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes: set[str] = set()

    def visit(body: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*prefix, node.name)
                nodes.add("::".join(qualified))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, qualified)

    visit(tree.body)
    return nodes


def _provenance_references(provenance: str) -> list[tuple[Path, str | None]]:
    references: list[tuple[Path, str | None]] = []
    for raw_reference in provenance.split(";"):
        reference = raw_reference.strip()
        path_text, separator, node = reference.partition("::")
        references.append((ROOT / path_text, node if separator else None))
    return references


def _source_family_markers(source: str) -> tuple[str, ...]:
    stem = Path(source).stem
    if stem.startswith("recipe_m2_scalar_"):
        return "recipe_m2_scalar_", stem.removeprefix("recipe_m2_scalar_")
    return (stem,)


def test_manifest_is_complete_and_snapshots_are_provenanced() -> None:
    assert MANIFEST["status"] == "collection-only; doc.patch execution pending"
    assert len(CASES) == 11
    assert len(CASES) == len(MANIFEST["cases"])

    for case in MANIFEST["cases"]:
        snapshot = CORPUS / case["fixture"]
        source = ROOT / case["source"]
        assert snapshot.is_file(), case["id"]
        assert source.is_file(), case["id"]
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        assert digest == case["source_sha256"], case["id"]
        assert digest == hashlib.sha256(source.read_bytes()).hexdigest(), case["id"]
        assert case["provenance"]
        assert case["tags"]
        assert case["expected"]["effect"]

        provenance_files = _provenance_references(case["provenance"])
        for provenance_path, node in provenance_files:
            assert provenance_path.is_file(), (case["id"], provenance_path)
            if node is not None:
                assert provenance_path.suffix == ".py", (case["id"], provenance_path)
                test_node = node.rsplit("::", 1)[-1].split("[", 1)[0]
                qualified_nodes = _qualified_python_nodes(provenance_path)
                qualified_node = "::".join((*node.split("::")[:-1], test_node))
                assert qualified_node in qualified_nodes, (case["id"], qualified_node)

        if case["source"].startswith("tests/editing_conformance/fixtures/"):
            markers = _source_family_markers(case["source"])
            referenced_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path, _ in provenance_files
                if path.suffix == ".py"
            )
            assert all(marker in referenced_text for marker in markers), case["id"]


@pytest.mark.parametrize(
    "case_id",
    [
        "nested-closure-trim",
        "created-track-and-clip",
        "params-absent",
        "params-null",
        "params-object-clear",
    ],
)
def test_real_canonical_documents_execute_the_recorded_edits(case_id: str) -> None:
    bundle, fixture = _canonical_bundle(case_id)
    command_selector = CASES[case_id]["edit"]
    commands = _selected(fixture, command_selector)
    if isinstance(commands, dict):
        commands = [commands]

    plan = execute(bundle, commands)
    if "output" in fixture:
        assert document_from_bundle(plan.bundle) == fixture["output"]
    else:
        assert plan.bundle == fixture["after_bundle"]
    assert plan.records


def test_disjoint_edit_rebases_and_preserves_the_real_head_change() -> None:
    bundle, fixture = _canonical_bundle("disjoint-rebase")
    command = fixture["command"]
    plan = execute(bundle, [command])
    rebased = replay(plan, fixture["head_bundle"])
    assert plan.bundle == fixture["after_bundle"]
    assert rebased.bundle == fixture["rebased_bundle"]
    assert State(rebased.bundle).entity("occurrence:o/clip:c")["params"]["y"] is False


def test_same_field_edit_is_a_real_refusal_case() -> None:
    bundle, fixture = _canonical_bundle("same-field-conflict")
    plan = execute(bundle, [fixture["commands"][0]])
    head = copy.deepcopy(bundle)
    head["parent"]["clips"][0]["from"] = 3
    head["parent"]["config"]["clips"][0]["from"] = 3

    with pytest.raises(CommandError, match="authored guard"):
        replay(plan, head)
    assert head["parent"]["clips"][0]["from"] == 3


def test_sdk_fixture_edit_is_loaded_and_reported_by_checkout() -> None:
    case = CASES["sdk-tiny3-text-edit"]
    checkout = Checkout(_fixture(case["id"]))
    before = copy.deepcopy(checkout.document())
    checkout.clip("c2-title").set(text="Two!")

    assert checkout._check_core().valid
    assert checkout.clip("c2-title").get("text") == "Two!"
    assert "c2-title" in {change["clip_id"] for change in checkout.edits()["changes"]}
    assert checkout.document() != before


def test_sdk_fixture_clear_preserves_opaque_metadata() -> None:
    checkout = Checkout(_fixture("sdk-publishable-clear"))
    clip = checkout.clip("c2.plate")
    app_before = copy.deepcopy(clip.data["app"])
    clip.clear_asset()

    assert checkout._check_core().valid
    assert clip.asset is None
    assert clip.data["app"] == app_before


def test_sample_project_is_loaded_by_the_core_timeline_loader(tmp_path: Path) -> None:
    source = CORPUS / CASES["sample-project-closure-reference"]["fixture"]
    config = load_timeline(source)
    assert [track["id"] for track in config["tracks"]] == ["v_manual", "v_cover", "a1"]
    assert {clip["id"] for clip in config["clips"]} >= {
        "clip_manual_image", "clip_cover_video", "clip_text", "clip_audio"
    }

    roundtrip = tmp_path / "sample_hype_full.json"
    save_timeline(config, roundtrip)
    assert json.loads(roundtrip.read_text(encoding="utf-8")) == json.loads(source.read_text(encoding="utf-8"))


def test_selected_param_archive_is_provenance_only() -> None:
    archive = CORPUS / CASES["sdk-selected-param-accepted-history"]["fixture"]
    with ZipFile(archive) as zipped:
        names = set(zipped.namelist())
        assert {"accepted.json", "backup/manifest.json", "backup/cas-manifest.json"} <= names
        accepted = json.loads(zipped.read("accepted.json"))
    assert accepted["accepted"]["data"]["status"] in {"accepted", "committed"}


def test_doc_patch_execution_is_explicitly_pending() -> None:
    assert not any("doc.patch" in json.dumps(case) for case in MANIFEST["cases"])
    assert all("doc.patch" not in case["expected"]["status"] for case in MANIFEST["cases"])
