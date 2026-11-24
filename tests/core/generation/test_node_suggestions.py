"""LE-5 independent lookup witnesses; recorded HTTP, no installation."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from astrid.core.generation.node_suggestions import missing_node_suggestions

SAM_URL = "https://github.com/ethanfel/ComfyUI-SAM-Audio"
SAM_CLASSES = ("SAMAudioPipelineLoader", "SAMAudioTextSeparator")


@pytest.fixture
def recorded_lookup(tmp_path, monkeypatch):
    from vibecomfy.registry import pack_resolver as resolver

    original = resolver.resolve_missing_nodes
    calls = []

    def install_fixture(*, found):
        def handle(request):
            path = request.url.path
            params = request.url.params
            if str(request.url).split("?", 1)[0] in (
                resolver.MANAGER_NODE_MAP_URL, resolver.MANAGER_NODE_LIST_URL,
                resolver.MANAGER_EXTENSION_MAP_URL,
            ):
                return httpx.Response(200, json={}, request=request)
            if path.endswith("/search/code") or path.endswith("/search/repositories"):
                return httpx.Response(200, json={"items": []}, request=request)
            if path.endswith("/nodes/search"):
                rows = []
                if found and params.get("search") == "SAM Audio":
                    rows = [{"id": "comfyui-sam-audio", "name": "ComfyUI-SAM-Audio",
                             "repository": SAM_URL}]
                return httpx.Response(200, json={"nodes": rows}, request=request)
            if path.endswith("/versions"):
                return httpx.Response(200, json=[], request=request)
            return httpx.Response(404, json={}, request=request)

        def lookup(query, *, query_intent):
            calls.append((query, query_intent))
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                return original(query, query_intent=query_intent, cache_root=tmp_path,
                                registry_client=client, manager_client=client,
                                github_client=client)

        monkeypatch.setattr(resolver, "resolve_missing_nodes", lookup)
        return calls

    return install_fixture


def _sam_missing_classes():
    fixture = Path(__file__).parent / "fixtures" / "le5-sam-audio-separation.api.json"
    workflow = json.loads(fixture.read_text(encoding="utf-8"))
    return sorted({node["class_type"] for node in workflow.values()}
                  - {"LoadAudio", "SaveAudio"})


@pytest.mark.parametrize("found", [True, False])
def test_sam_recorded_lookup_is_suggestion_not_install(recorded_lookup, found):
    calls = recorded_lookup(found=found)
    classes = _sam_missing_classes()
    assert tuple(classes) == SAM_CLASSES
    result = missing_node_suggestions(SimpleNamespace(), [*classes, classes[0]])
    assert calls == [(name, "class_name") for name in SAM_CLASSES] + [("SAM Audio", "capability")]
    assert result["runnable"] is False
    assert all(lookup["runnable"] is False for lookup in result["lookups"])
    if found:
        row, = result["candidates"]
        assert row["url"] == SAM_URL
        assert row["label"] == "name match only"
        assert row["add_option"] == ["--add", SAM_URL]
        assert row["runnable"] is False
        assert row["evidence"]
        assert "Choose and verify" in result["instruction"]
    else:
        assert result["candidates"] == []
        assert "manually" in result["instruction"]
    assert all("argv" not in row for row in result["candidates"])


@pytest.mark.parametrize("key", ["cnr_id", "aux_id"])
def test_ui_provenance_precedes_class_name_guess(monkeypatch, key):
    from vibecomfy.registry import pack_resolver as resolver

    calls = []

    def lookup(query, *, query_intent):
        calls.append((query, query_intent))
        return resolver.MissingNodeResolution(query, query_intent)

    monkeypatch.setattr(resolver, "resolve_missing_nodes", lookup)
    report = {"provenance": {"source_provenance": {"records": [
        {"class_type": "UnrelatedNode", "execution_looking": True, key: "authored-pack"},
        {"class_type": "AnotherNode", "execution_looking": True, key: "authored-pack"},
        {"class_type": "NotMissing", "execution_looking": True, key: "ignore-pack"},
    ]}}}
    workflow = SimpleNamespace(_astrid_import_report=report)
    result = missing_node_suggestions(workflow, ["UnrelatedNode", "AnotherNode"])
    assert calls == [("AnotherNode", "class_name"), ("UnrelatedNode", "class_name"),
                     ("authored-pack", "capability")]
    assert result["candidates"] == []
    assert workflow._astrid_import_report is report
