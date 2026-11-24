"""LE-10 CPU stand-in: selected pack evidence, then actual live doctor.

The two Git revisions are schema-only fixture packs. No third-party code,
weights, target install or remote machine are used by this proof.
"""
from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrid.core.generation.entries import target_schema
from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.preflight import generation_error, readiness_envelope
from tests.core.generation.test_owned_doctor_schema import (  # noqa: F401
    _native, captured_schema, latent_workflow, native_python, schema_server,
)
from tests.core.generation.test_preparation_step_logs import _preparation
from vibecomfy.porting.provenance import extract_provenance
from vibecomfy.security.provenance import Provenance
from vibecomfy.workflow_bundle import load_bundle


def test_two_commit_schema_fixture_warns_then_doctor_names_removed_input(
        native_python, schema_server, latent_workflow, tmp_path):
    root = tmp_path / "fixture-pack"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, text=True,
            capture_output=True,
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Schema fixture")
    git("config", "user.email", "schema-fixture@example.test")
    path = root / "object_info.json"
    path.write_text(json.dumps(schema_server.info), encoding="utf-8")
    git("add", "object_info.json")
    git("commit", "-m", "Original input")
    old = git("rev-parse", "HEAD")
    changed = copy.deepcopy(schema_server.info)
    inputs = changed["EmptyLatentImage"]["input"]["required"]
    inputs["renamed_height"] = inputs.pop("height")
    path.write_text(json.dumps(changed), encoding="utf-8")
    git("add", "object_info.json")
    git("commit", "-m", "Rename height")
    selected = git("rev-parse", "HEAD")
    assert old != selected

    graph = load_bundle(latent_workflow, trust=Provenance.USER_CONFIRMED).workflow
    api = graph.compile("api")
    labels = tuple((str(node_id), row["class_type"], name)
                   for node_id, row in api.items() for name in row.get("inputs", {}))
    node_id = next(node for node, cls, field in labels
                   if cls == "EmptyLatentImage" and field == "height")
    # The source UI receipt and selected fixture SHA describe the same node.
    graph.metadata["source_provenance"] = extract_provenance({"nodes": [
        {"id": node_id, "type": "EmptyLatentImage",
         "properties": {"cnr_id": "fixture-pack", "ver": old}},
    ]}).to_json()
    graph.requirements = replace(
        graph.requirements, custom_nodes=["fixture-pack"],
        custom_node_refs=[{"slug": "fixture-pack", "source": "git",
                           "url": "https://example.test/fixture-pack",
                           "commit": selected, "class_set": ["EmptyLatentImage"]}],
    )
    notices = []
    pending = {"engine": "comfyui", "placement": "runpod"}
    assert target_schema({"id": "fixture"}, graph, None,
                         pending_target=pending, align="align", version_notices=notices) is None
    notice, = notices
    preview = readiness_envelope(issues=[notice["issue"]], target=pending)
    assert preview["status"] == "ready_with_warnings"
    assert preview["issues"][0]["code"] == "pack_version_differs"
    assert preview["issues"][0]["details"]["recorded_versions"] == [old]
    assert preview["issues"][0]["details"]["selected_commit"] == selected

    # Simulate a target exposing precisely the selected fixture's schema.
    schema_server.info = json.loads(git("show", selected + ":object_info.json"))
    result = _native(native_python, schema_server, latent_workflow, tmp_path)
    assert result.returncode == 1
    prep = _preparation(tmp_path, None)
    prep._workflow_input_labels = labels
    prep.context = SimpleNamespace(run=lambda request: SimpleNamespace(
        exit_code=result.returncode, timed_out=False,
        stdout=result.stdout, stderr=result.stderr,
    ))
    with pytest.raises(IntegrityError) as caught:
        prep._native("recorded native doctor result", step="doctor")
    error = generation_error(caught.value).to_envelope()
    assert error["status"] == "blocked"
    issue = next(row for row in error["issues"] if row["code"] == "unknown_input")
    assert issue["details"] == {"node_id": node_id, "class_type": "EmptyLatentImage",
                                "input": "height", "field": "height"}
    fix, = error["fixes"]
    assert issue["fix_ids"] == [fix["id"]]
    assert fix["who"] == "human"
    assert fix["instruction"]["then_argv"] == [
        "astrid", "generate", "describe", "<WORKFLOW>", "--json",
    ]
    assert error["attempt_count"] == 1
