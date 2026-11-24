"""LE-16 independent group and combined-choice witnesses."""
from __future__ import annotations

import pytest

from astrid.core.generation.node_suggestions import (
    combined_pack_choice,
    missing_node_pack_suggestions,
)


@pytest.mark.parametrize("authored", [False, True])
def test_two_missing_pack_groups_lookup_once_and_one_human_choice(monkeypatch, authored):
    from vibecomfy.registry import pack_resolver as resolver

    classes = ["AlphaPackLoader", "AlphaPackSampler", "BetaToolsLoader", "BetaToolsOutput"]
    calls = []

    def lookup(query, *, query_intent):
        calls.append((query, query_intent))
        candidates = ()
        if query_intent == "capability":
            slug = "alpha" if query.startswith(("Alpha", "alpha")) else "beta"
            candidates = (resolver.ResolverCandidate(
                ref=resolver.PackRef(slug=slug, source="github",
                                     url=f"https://github.com/example/{slug}"),
            ),)
        return resolver.MissingNodeResolution(query, query_intent, candidates=candidates)

    monkeypatch.setattr(resolver, "resolve_missing_nodes", lookup)
    report = None
    if authored:
        report = {"provenance": {"source_provenance": {"records": [
            {"class_type": name, "execution_looking": True,
             "cnr_id": "alpha-pack" if name.startswith("Alpha") else "beta-tools"}
            for name in classes
        ]}}}
    groups = missing_node_pack_suggestions([*classes, classes[0]], import_report=report)
    assert [group["classes"] for group in groups] == [classes[:2], classes[2:]]
    assert all(group["group_kind"] == ("authored" if authored else "provisional") for group in groups)
    assert [query for query, intent in calls if intent == "class_name"] == classes
    assert [query for query, intent in calls if intent == "capability"] == (
        ["alpha-pack", "beta-tools"] if authored else ["Alpha Pack", "Beta Tools"]
    )
    assert all(group["runnable"] is False for group in groups)
    assert all(group["candidates"][0]["label"] == "name match only" for group in groups)
    fix = combined_pack_choice(groups, retry_argv=[
        "astrid", "generate", "workflow", "source.json", "--as-entry", "example",
        "--placement", "runpod", "--dry-run", "--consent", "code=old",
    ])
    assert fix["who"] == "human" and "argv" not in fix
    argv = fix["instruction"]["then_argv"]
    assert argv.count("--add") == 2
    assert "<MISSING_PACK_1>" in argv and "<MISSING_PACK_2>" in argv
    assert "--dry-run" not in argv and "code=old" not in argv
    assert argv[argv.index("--placement") + 1] == "runpod"
    assert set(fix["placeholders"]) == {"MISSING_PACK_1", "MISSING_PACK_2"}


def test_conflicting_authored_provenance_never_coalesces(monkeypatch):
    from vibecomfy.registry import pack_resolver as resolver

    calls = []

    def lookup(query, *, query_intent):
        calls.append((query, query_intent))
        return resolver.MissingNodeResolution(query, query_intent)

    monkeypatch.setattr(resolver, "resolve_missing_nodes", lookup)
    report = {"provenance": {"source_provenance": {"records": [
        {"class_type": "SamePrefixOne", "cnr_id": "one", "execution_looking": True},
        {"class_type": "SamePrefixOne", "cnr_id": "other", "execution_looking": True},
        {"class_type": "SamePrefixTwo", "cnr_id": "two", "execution_looking": True},
    ]}}}
    groups = missing_node_pack_suggestions(["SamePrefixOne", "SamePrefixTwo"], import_report=report)
    assert [group["classes"] for group in groups] == [["SamePrefixOne"], ["SamePrefixTwo"]]
    assert calls == [("SamePrefixOne", "class_name"), ("SamePrefixTwo", "class_name"), ("two", "capability")]
    assert all(group["candidates"] == [] for group in groups)


def test_grouped_discovery_reuses_one_query_only_within_one_run(monkeypatch):
    from vibecomfy.registry import pack_resolver as resolver

    calls = []

    def lookup(query, *, query_intent):
        calls.append((query, query_intent))
        return resolver.MissingNodeResolution(query, query_intent)

    monkeypatch.setattr(resolver, "resolve_missing_nodes", lookup)
    # Distinct groups can derive the same name query. Their class evidence
    # remains separate but the repeated registry search must be reused.
    report = {"provenance": {"source_provenance": {"records": [
        {"class_type": "AlphaPackLoader", "cnr_id": "Alpha Pack", "execution_looking": True},
    ]}}}
    classes = ["AlphaPackLoader", "AlphaPackSampler"]
    expected = [
        ("AlphaPackLoader", "class_name"),
        ("Alpha Pack", "capability"),
        ("AlphaPackSampler", "class_name"),
    ]
    first = missing_node_pack_suggestions(classes, import_report=report)
    assert [row["group_kind"] for row in first] == ["authored", "provisional"]
    assert calls == expected
    assert all(row["lookups"][-1]["query"] == "Alpha Pack" for row in first)
    second = missing_node_pack_suggestions(classes, import_report=report)
    assert second == first
    assert calls == expected + expected
