"""LE-5 real-path acceptance witnesses, staged before target_schema handoff.

Only the public HTTP lookup is recorded. The CLI, canonical import and
target admission are real. The candidate cannot authorize installation.
"""
from __future__ import annotations

import json

import httpx
import pytest

from astrid.core.generation import cli, entries, node_suggestions, preflight
from astrid.packs.vibecomfy.production_engine import load_workflow_path
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)
from tests.core.generation.fixtures.plan56_sam import (  # noqa: F401
    sam_api_path, sam_object_info, sam_offline_lookup,
)

SAM_CLASSES = {"SAMAudioPipelineLoader", "SAMAudioTextSeparator"}
SAM_URL = "https://github.com/ethanfel/ComfyUI-SAM-Audio"


@pytest.fixture
def sam_name_search(monkeypatch, sam_offline_lookup):
    """Extend the shared empty HTTP replay with one recorded name-only hit."""
    send = httpx.Client.send

    def install(*, found):
        def replay(client, request, *args, **kwargs):
            if request.url.host == "api.comfy.org":
                if request.url.path == "/nodes/search" and request.url.params.get("search") == "SAM Audio":
                    rows = [{"id": "comfyui-sam-audio", "name": "ComfyUI-SAM-Audio",
                             "repository": SAM_URL}] if found else []
                    return httpx.Response(200, json={"nodes": rows}, request=request)
                if request.url.path == "/nodes/comfyui-sam-audio/versions":
                    return httpx.Response(200, json=[], request=request)
            return send(client, request, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "send", replay)

    return install


def _missing_pack_issues(result):
    issues = [issue for issue in result["issues"] if issue["code"] == "missing_node_pack"]
    assert {issue["details"]["node"] for issue in issues} == SAM_CLASSES
    return issues


def _linked_fixes(result, issues):
    by_id = {fix["id"]: fix for fix in result["fixes"] if "id" in fix}
    for issue in issues:
        assert issue["fix_ids"], "missing_node_pack must link a next step"
        assert set(issue["fix_ids"]) <= by_id.keys()
    return [by_id[fix_id] for fix_id in dict.fromkeys(
        fix_id for issue in issues for fix_id in issue["fix_ids"]
    )]


@pytest.mark.parametrize("placement", ["runpod", "local"])
@pytest.mark.parametrize("found", [True, False])
def test_le5_cli_deferred_sam_reports_evidence_without_install(
    placement, found, monkeypatch, tmp_path, capsys, sam_api_path, sam_name_search,
):
    from astrid.core.generation import placement as placements
    from astrid.sdk import generation

    sam_name_search(found=found)
    monkeypatch.setattr(placements, "_runpod_leases", lambda kind: [])
    monkeypatch.setattr(preflight, "_registered_managed_engine", lambda: None)
    monkeypatch.setattr(generation, "_require_claimed_machine",
                        lambda *a, **k: pytest.fail("dry run required a claimed machine"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry run opened Runtime"))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    original = sam_api_path.read_bytes()
    observed = []
    lookup = node_suggestions.missing_node_suggestions

    def record(missing_classes, *, import_report):
        assert import_report is not None, "raw JSON import must pass its producer report"
        result = lookup(missing_classes, import_report=import_report)
        observed.append(result)
        return result

    monkeypatch.setattr(node_suggestions, "missing_node_suggestions", record)
    assert cli.main(["workflow", str(sam_api_path), "--placement", placement,
                     "--dry-run", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert result["target"]["placement"] == placement
    issues = _missing_pack_issues(result)
    assert sam_api_path.read_bytes() == original
    assert not (tmp_path / "entries").exists()
    assert len([issue for issue in result["issues"] if issue["code"] == "engine_not_found"]) == (
        1 if placement == "local" else 0
    )
    assert len(observed) == 1, "readiness must use the lookup helper once for the missing classes"
    lookup_result = observed[0]
    assert set(lookup_result["classes"]) == SAM_CLASSES
    assert lookup_result["runnable"] is False
    assert [(row["query"], row["query_intent"]) for row in lookup_result["lookups"]] == [
        ("SAMAudioPipelineLoader", "class_name"), ("SAMAudioTextSeparator", "class_name"),
        ("SAM Audio", "capability"),
    ]
    fixes = _linked_fixes(result, issues)
    assert all("argv" not in fix for fix in fixes), "no guessed executable repair"
    assert all(fix["who"] == "human" for fix in fixes)
    assert all("--dry-run" not in fix["instruction"]["then_argv"] for fix in fixes)
    public = json.dumps({"issues": issues, "fixes": fixes})
    if found:
        assert SAM_URL in public and "name match only" in public
        assert "evidence" in public
        assert "--add" in public
    else:
        assert SAM_URL not in public
        assert "manually" in public.lower()
    assert all(fix["instruction"]["then_argv"] for fix in fixes)


def test_le5_attached_sam_live_inventory_remains_authoritative(
    network, monkeypatch, tmp_path, sam_api_path, sam_name_search,
):
    from vibecomfy.entries import draft_entry
    from astrid.core.generation.preflight import GenerationReadinessError
    from astrid.sdk.generation import _explicit_workflow_needs

    sam_name_search(found=True)
    graph = load_workflow_path(sam_api_path, tmp_path / "canonical").resolved.workflow
    entry = draft_entry(graph, "sam")
    entry["needs"] = _explicit_workflow_needs(entry["needs"])
    info = sam_object_info()
    missing, _ = install_target(network, {k: v for k, v in info.items() if k not in SAM_CLASSES})
    with pytest.raises(GenerationReadinessError) as caught:
        entries.target_schema(entry, graph, missing)
    blocked = caught.value.readiness
    assert blocked["status"] == "blocked"
    issues = _missing_pack_issues(blocked)
    for fix in _linked_fixes(blocked, issues):
        assert "argv" not in fix
        assert fix["who"] == "human"
        assert fix["scope"] == "attached_target"
        assert "--add" not in fix["instruction"]["then_argv"]

    # A completely loaded target must not look up or install anything.
    monkeypatch.setattr(node_suggestions, "missing_node_suggestions",
                        lambda *a, **k: pytest.fail("lookup ran for a ready attached target"))
    ready, _ = install_target(network, info)
    assert entries.target_schema(entry, graph, ready) is not None
