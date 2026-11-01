"""Canonical transition preparation requires Runtime source authority."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.rendering.rendering.renderers.remotion import root_preparation


def test_exact_typed_parent_capture_receives_compiled_transition_vector(monkeypatch):
    project = Path(__file__).resolve().parents[3] / "remotion"
    vector = {"fps": 24, "tracks": [{"trackId": "v", "clipIds": ["a", "b"],
                                      "incomingTransitions": [None, {"T": 12}]}]}
    source = SimpleNamespace(read=lambda: {
        "ref": {"source_kind": "revision"},
        "transition_contract": "runtime-parent-closure/v1",
    }, validate=lambda *_: None)
    packs_path = str(Path("/tmp/approved-external-packs"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", packs_path)
    monkeypatch.setattr(root_preparation, "probe_context", lambda **_kwargs: {"node_env": {}})
    monkeypatch.setattr("astrid.core.rendering.verified_source.inherited_source",
                        lambda *_: source)

    def compile_transitions(command, *, input, **kwargs):
        assert command[1] == str(project / "scene-query.mjs")
        assert kwargs["env"]["ASTRID_PACKS_PATH"] == packs_path
        payload = json.loads(input)
        assert payload["operation"] == "compile-canonical-transitions"
        assert payload["props"]["timeline"]["app"]["occurrence_id"] == "spoof-is-opaque"
        assert payload["basis"] == {"fps": 24}
        return SimpleNamespace(returncode=0, stdout=json.dumps(vector), stderr="")

    monkeypatch.setattr(root_preparation.subprocess, "run", compile_transitions)
    props = {"timeline": {"app": {"occurrence_id": "spoof-is-opaque"}}}
    root_preparation._compile_verified_canonical_transitions(
        props, fps=24, timeline_path=Path("timeline.json"), assets_path=Path("assets.json"),
        runtime_tools=SimpleNamespace(node_executable=Path("/node")), project_dir=project,
    )
    assert props["compiledTransitions"] == vector


@pytest.mark.parametrize("app_metadata", [
    {"canonicalTiming": True}, {"occurrence_id": "runtime-looking"},
])
def test_native_or_spoofed_metadata_keeps_legacy_transition_mode(app_metadata, monkeypatch):
    monkeypatch.setattr("astrid.core.rendering.verified_source.inherited_source", lambda *_: None)
    monkeypatch.setattr(root_preparation.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("unverified input must not compile transitions"))
    props = {"timeline": {"app": app_metadata}}
    root_preparation._compile_verified_canonical_transitions(
        props, fps=24, timeline_path=Path("timeline.json"), assets_path=Path("assets.json"),
        runtime_tools=SimpleNamespace(node_executable=Path("/node")),
        project_dir=Path(__file__).resolve().parents[3] / "remotion",
    )
    assert "compiledTransitions" not in props


@pytest.mark.parametrize("source_kind", ["revision", "working_copy"])
def test_verified_native_revision_or_working_copy_stays_legacy_without_contract(source_kind, monkeypatch):
    source = SimpleNamespace(read=lambda: {
        "ref": {"source_kind": source_kind},
        "authority": {"expansion": {"occurrences": [{"occurrence_id": "runtime-looking"}]}},
    })
    monkeypatch.setattr("astrid.core.rendering.verified_source.inherited_source", lambda *_: source)
    monkeypatch.setattr(root_preparation.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("source without the typed capture contract must not compile"))
    props = {"timeline": {"clips": []}}
    root_preparation._compile_verified_canonical_transitions(
        props, fps=24, timeline_path=Path("timeline.json"), assets_path=Path("assets.json"),
        runtime_tools=SimpleNamespace(node_executable=Path("/node")),
        project_dir=Path(__file__).resolve().parents[3] / "remotion",
    )
    assert "compiledTransitions" not in props


def test_verified_binding_mints_transition_contract_only_from_matching_typed_capture(tmp_path):
    from astrid.core.rendering.verified_source import mint_prepared_source

    attempt = tmp_path / "attempt"
    inputs = attempt / "inputs"
    inputs.mkdir(parents=True)
    timeline_path = inputs / "timeline.json"
    assets_path = inputs / "registry.json"
    timeline_path.write_text('{"clips": []}', encoding="utf-8")
    assets_path.write_text('{"assets": {}}', encoding="utf-8")
    digest = "sha256:" + "a" * 64
    authority = {
        "project_id": "p", "timeline_id": "t", "parent_revision_id": "r",
        "parent_content_digest": digest,
        "expansion": {
            "parent_revision_id": "r", "parent_content_digest": digest,
            "graph": {}, "occurrences": [], "shots": [], "children": [],
        },
    }
    binding_source = mint_prepared_source(
        ref={"project_id": "p", "timeline_id": "t", "revision_id": "r",
             "source_kind": "revision", "revision_digest": digest},
        authority=authority,
        snapshot={"config": {"clips": []}, "registry": {"assets": {}},
                  "metadata": {"capture_authority": authority,
                               "host_transition_contract": "runtime-parent-closure/v1"}},
        timeline_path=timeline_path, assets_path=assets_path, attempt=attempt,
    )
    assert binding_source.read()["transition_contract"] == "runtime-parent-closure/v1"

    working_attempt = tmp_path / "working-copy-attempt"
    working_inputs = working_attempt / "inputs"
    working_inputs.mkdir(parents=True)
    working_timeline = working_inputs / "timeline.json"
    working_registry = working_inputs / "registry.json"
    working_timeline.write_text('{"clips": []}', encoding="utf-8")
    working_registry.write_text('{"assets": {}}', encoding="utf-8")
    candidate_digest = "sha256:" + "b" * 64
    publication_digest = "sha256:" + "c" * 64
    base_digest = "sha256:" + "d" * 64
    working_authority = {
        "project_id": "p", "timeline_id": "t", "parent_revision_id": "r",
        "parent_content_digest": publication_digest,
        "expansion": {
            "parent_revision_id": "r", "parent_content_digest": publication_digest,
            "graph": {}, "occurrences": [], "shots": [], "children": [],
            "working_copy": {
                "candidate_parent_revision_id": "r",
                "candidate_digest": candidate_digest,
                "publication_digest": publication_digest,
                "base_parent": {"revision_id": "base", "content_digest": base_digest},
            },
        },
    }
    working_marker = working_authority["expansion"]["working_copy"]
    working_source = mint_prepared_source(
        ref={"project_id": "p", "timeline_id": "t", "revision_id": "r",
             "source_kind": "working_copy", "revision_digest": candidate_digest,
             "working_copy": {"candidate_digest": candidate_digest,
                              "publication_digest": publication_digest,
                              "base_revision_id": "base", "base_revision_digest": base_digest}},
        authority=working_authority,
        snapshot={"config": {"clips": []}, "registry": {"assets": {}},
                  "metadata": {"capture_authority": working_authority,
                               "working_copy": working_marker,
                               "host_transition_contract": "runtime-parent-closure/v1"}},
        timeline_path=working_timeline, assets_path=working_registry, attempt=working_attempt,
    )
    assert working_source.read()["transition_contract"] == "runtime-parent-closure/v1"


def test_native_revision_with_lookalike_host_metadata_keeps_legacy_contract(tmp_path):
    from astrid.core.rendering.verified_source import mint_prepared_source

    attempt = tmp_path / "native-attempt"
    inputs = attempt / "inputs"
    inputs.mkdir(parents=True)
    timeline_path = inputs / "timeline.json"
    assets_path = inputs / "registry.json"
    timeline_path.write_text('{"clips": []}', encoding="utf-8")
    assets_path.write_text('{"assets": {}}', encoding="utf-8")
    digest = "sha256:" + "a" * 64
    authority = {
        "project_id": "p", "timeline_id": "t", "parent_revision_id": "r",
        "parent_content_digest": digest, "expansion": {},
    }
    source = mint_prepared_source(
        ref={"project_id": "p", "timeline_id": "t", "revision_id": "r",
             "source_kind": "revision", "revision_digest": digest},
        authority=authority,
        snapshot={"config": {"clips": []}, "registry": {"assets": {}},
                  "metadata": {"host_transition_contract": "runtime-parent-closure/v1"}},
        timeline_path=timeline_path, assets_path=assets_path, attempt=attempt,
    )
    assert "transition_contract" not in source.read()
