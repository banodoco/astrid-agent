"""B9.1 source reconciliation and no-drop coverage."""

import json
from pathlib import Path

from astrid.core.execution.capability_ledger import load_capability_ledger


def test_shipped_ledger_reconciles_historical_capability_sets():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    sources = ledger["sources"]

    assert sources["counts"]["pack_labels"] == 107
    assert sources["counts"]["historical_pack_labels"] == 108
    # The executor inventory is the committed output-result snapshot plus historical
    # rows; it grows whenever a bundled executor is registered, so pin it to the
    # approved census lock rather than a literal.
    lock = json.loads(Path("config/astrid-capability-census.lock.json").read_text(encoding="utf-8"))
    assert sources["counts"]["executor_inventory"] == len(lock["executor_inventory"])
    assert all(section["complete"] for section in sources["coverage"].values())
    assert not sources["coverage"]["source_labels"]["missing"]
    assert sources["coverage"]["historical_source_labels"]["complete"]
    assert not sources["coverage"]["executor_inventory"]["missing"]
    assert not {
        row["pack"]
        for row in sources["pack_labels"]
        if row["pack"] in {"discord_local", "seedance_local"}
    }
    finalizer_label = next(
        row for row in sources["pack_labels"]
        if row["pack"] == "h3_av" and row["label"] == "finalize_publication"
    )
    assert finalizer_label["source"] == "astrid/packs/h3_av/pack.yaml"
    assert finalizer_label["disposition"] == "unmapped_source_label"
    finalizer_executor = next(
        row for row in sources["executor_inventory"]
        if row["id"] == "h3_av.publication_finalizer"
    )
    assert finalizer_executor["result_contract"] == "manifest"
    assert finalizer_executor["disposition"] == "advertised"
    assert finalizer_executor["discovery_status"] == "discovered"


def test_ledger_has_only_canonical_ids_models_backends_and_explicit_unmapped_labels():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    sources = ledger["sources"]

    assert sources["aliases"] == []
    assert sources["models"]
    assert {"local", "cloud"} <= set(sources["generation_backends"])
    assert {row["id"] for row in sources["rendering_backends"]} >= {"rendering.ffmpeg", "rendering.remotion", "rendering.threejs"}
    assert any(row["disposition"] == "unmapped_source_label" for row in sources["pack_labels"])


def test_host_consumes_the_reconciled_ledger_before_readiness_matrix():
    from astrid.core.execution.generic_host import GenericPackHost

    host = GenericPackHost(pack_roots=[Path("astrid/packs")])
    assert host.ledger["sources"]["counts"]["pack_labels"] == 107
    # The host matrix is the shipped readiness matrix, row for row; deriving the
    # count keeps it from drifting each time a capability is admitted.
    shipped = json.loads(Path("config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    assert set(host.matrix) == {row["id"] for row in shipped["capabilities"]}
    assert len(host.matrix) == len(shipped["capabilities"])
    finalizer = host.matrix["h3_av.publication_finalizer"]
    assert finalizer["adapter_family"] == "cpu"
    assert finalizer["resource_keys"] == ["cpu"]


def test_vibecomfy_readiness_reserves_gpu_for_workflow_execution():
    from astrid.core.execution.generic_host import GenericPackHost

    host = GenericPackHost(
        pack_roots=[Path("astrid/packs/vibecomfy")],
        capability_matrix=Path("config/astrid-beta-capabilities.json"),
    )
    for capability in (
        "vibecomfy.import",
        "vibecomfy.inspect",
        "vibecomfy.edit",
        "vibecomfy.validate",
    ):
        entry = host.matrix[capability]
        assert entry["adapter_family"] == "cpu"
        assert entry["resource_keys"] == ["cpu"]

    run = host.matrix["vibecomfy.run"]
    assert run["adapter_family"] == "local_generation"
    assert run["resource_keys"] == ["gpu"]


def test_historical_executor_rows_are_explicitly_not_installed():
    """Historical rows must not be mistaken for executable local routes."""
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    rows = ledger["sources"]["executor_inventory"]
    absent = {row["id"]: row for row in rows if row["discovery_status"] != "discovered"}

    assert set(absent) == {
        "iteration.prepare",
        "training.asset_cache",
    }
    external = {row["id"]: row for row in absent.values() if row["id"].split(".", 1)[0] in {"discord_local", "hivemind", "seedance_local"}}
    historical = {row["id"]: row for row in absent.values() if row["id"] not in external}
    assert external == {}
    assert {row["disposition"] for row in historical.values()} == {"retired"}
    assert {row["discovery_status"] for row in historical.values()} == {"historical_only"}
    assert all(row["executable"] is False for row in historical.values())


def test_historical_source_labels_preserve_retirements_and_replacement():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    rows = {f"{row['pack']}.{row['label']}": row for row in ledger["sources"]["historical_pack_labels"]}

    assert rows["iteration.collect_thread_provenance"]["disposition"] == "replaced"
    assert rows["iteration.collect_thread_provenance"]["equivalent_to"] == "iteration.collect_runtime_provenance"
    assert rows["iteration.collect_thread_provenance"]["executable"] is False
    assert rows["typed_timeline.typed_timeline.render"]["disposition"] == "retired"


def test_hivemind_census_keeps_the_external_contract_without_claiming_installation():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    census = ledger["sources"]["hivemind"]["external_census"]

    assert census == {
        "declared_count": 7,
        "installed_count": 0,
        "unresolved": True,
        "note": "Seven Hivemind executors are declared by the optional external pack; installation is proven by the managed source inventory.",
    }


def test_hivemind_matrix_rows_expose_provider_readiness_metadata():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    rows = {row["id"]: row for row in ledger["capabilities"] if row["id"].startswith("hivemind.")}
    assert set(rows) == {
        "hivemind.contribute", "hivemind.get_item", "hivemind.ingest_article",
        "hivemind.ingest_workflow", "hivemind.ingest_youtube",
        "hivemind.refresh_media", "hivemind.search",
    }
    for row in rows.values():
        assert row["adapter_family"] == "provider"
        assert row["resource_keys"] == ["provider"]
        assert row["disposition"] == "optional"
    # The public endpoint and anon key have safe baked-in defaults; readiness
    # must not require users to export overrides for read-only access.
    assert rows["hivemind.search"]["required_env"] == []
    assert rows["hivemind.get_item"]["required_env"] == []
    assert "HIVEMIND_CONTRIBUTOR_KEY" in rows["hivemind.contribute"]["required_env"]


def test_source_census_reports_unreviewed_pack_labels_without_failing_host(monkeypatch):
    """Census drift is named and reported; it does not stop the host from starting."""
    from astrid.core.execution import capability_ledger

    original = capability_ledger._census_sets

    def with_unreviewed_label(repo_root):
        census = original(repo_root)
        census["current"]["source_labels"].add("hivemind.unreviewed")
        return census

    monkeypatch.setattr(capability_ledger, "_census_sets", with_unreviewed_label)
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    row = ledger["sources"]["coverage"]["source_labels"]
    assert row["status"] == "drift"
    assert row["added"] == ["hivemind.unreviewed"]
    assert row["complete"] is False


def test_hivemind_matrix_contract_is_not_mistaken_for_bundled_source():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    assert not any(row["pack"] == "hivemind" for row in ledger["sources"]["pack_labels"])
    assert ledger["sources"]["hivemind"]["disposition"] == "optional_external"
    assert len(ledger["sources"]["hivemind"]["executor_ids"]) == 7
    matrix_ids = {row["id"] for row in ledger["capabilities"]}
    assert set(ledger["sources"]["hivemind"]["executor_ids"]) <= matrix_ids


def test_reviewed_v3_pack_labels_preserve_exact_canonical_action_admission():
    from astrid.core.execution.capability_ledger import capability_execution_admission

    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    labels = {
        (row["pack"], row["label"]): row
        for row in ledger["sources"]["pack_labels"]
        if row["pack"] in {"capture", "chiptune", "pixel"}
    }
    expected = {
        ("capture", "capture_web_page"): ("capture.web_page", "provider", ["provider"]),
        ("chiptune", "compose"): ("chiptune.compose", "cpu", ["cpu"]),
        ("chiptune", "sfx"): ("chiptune.sfx", "cpu", ["cpu"]),
        ("pixel", "cutout"): ("pixel.cutout", "cpu", ["cpu"]),
        ("pixel", "snap"): ("pixel.snap", "cpu", ["cpu"]),
        ("pixel", "sprites"): ("pixel.sprites", "cpu", ["cpu"]),
        ("chiptune", "midi"): ("chiptune.midi", "cpu", ["cpu"]),
        ("pixel", "strip"): ("pixel.strip", "cpu", ["cpu"]),
    }
    # pixel.strip now has a reviewed matrix row (4b77bdcf), so it is admitted like its siblings.
    assert set(labels) == set(expected)
    matrix = {row["id"]: row for row in ledger["capabilities"]}
    for (pack, label), (action_id, adapter, resources) in expected.items():
        source = labels[(pack, label)]
        assert source["source"] == f"astrid/packs/{pack}/pack.yaml"
        assert matrix[action_id]["disposition"] == "optional"
        assert matrix[action_id]["adapter_family"] == adapter
        assert matrix[action_id]["resource_keys"] == resources
        admission = capability_execution_admission(ledger, action_id)
        assert admission["executable"] is True
        assert admission["requires_runtime_readiness"] is True
        if pack == "capture":
            assert source["canonical_id"] is None
            assert source["disposition"] == "unmapped_source_label"
        else:
            assert source["canonical_id"] == action_id
            assert source["disposition"] == "advertised"


def test_reviewed_v3_source_occurrence_removal_is_reported_by_name(monkeypatch):
    from astrid.core.execution import capability_ledger

    original = capability_ledger._census_sets

    def without_reviewed_occurrence(repo_root):
        census = original(repo_root)
        census["current"]["source_labels"].discard("pixel.snap")
        return census

    monkeypatch.setattr(capability_ledger, "_census_sets", without_reviewed_occurrence)
    row = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))["sources"]["coverage"]["source_labels"]
    assert row["status"] == "drift"
    assert row["missing"] == ["pixel.snap"]
