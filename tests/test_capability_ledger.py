"""B9.1 source reconciliation and no-drop coverage."""

import json
from pathlib import Path

from astrid.core.execution.capability_ledger import load_capability_ledger


def test_shipped_ledger_reconciles_historical_capability_sets():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    sources = ledger["sources"]
    coverage = sources["coverage"]

    # The approved census lock is the core-only set, so the installed packs
    # reconcile completely with no drift.
    assert sources["counts"]["pack_labels"] == 35
    assert sources["counts"]["historical_pack_labels"] == 39
    lock = json.loads(Path("config/astrid-capability-census.lock.json").read_text(encoding="utf-8"))
    assert sources["counts"]["executor_inventory"] == len(lock["executor_inventory"]) == 51
    assert len(lock["source_labels"]) == 35
    assert len(lock["historical_source_labels"]) == 39
    assert all(section["complete"] for section in coverage.values())
    for section in ("source_labels", "historical_source_labels", "executor_inventory"):
        assert coverage[section]["status"] == "ok"
        assert coverage[section]["added"] == [] and coverage[section]["missing"] == []
    assert not {
        row["pack"]
        for row in sources["pack_labels"]
        if row["pack"] in {"discord_local", "seedance_local"}
    }


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

    host = GenericPackHost(
        pack_roots=[Path("astrid/packs")],
        capability_matrix=Path("config/astrid-beta-capabilities.json"),
    )
    assert host.ledger["sources"]["counts"]["pack_labels"] == 35
    # The host matrix is the shipped readiness matrix, row for row; deriving the
    # count keeps it from drifting each time a capability is admitted.
    shipped = json.loads(Path("config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    assert set(host.matrix) == {row["id"] for row in shipped["capabilities"]}
    assert len(host.matrix) == len(shipped["capabilities"])


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


HIVEMIND_IDS = {
    "hivemind.contribute", "hivemind.get_item", "hivemind.ingest_article",
    "hivemind.ingest_workflow", "hivemind.ingest_youtube",
    "hivemind.refresh_media", "hivemind.search",
}


def test_hivemind_is_an_installed_pack_not_an_external_contract():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    sources = ledger["sources"]

    # Hivemind ships in-tree now, so the matrix declares no external contract and
    # all seven executors are discovered rather than reported not-installed.
    assert "hivemind" not in sources
    rows = {row["id"]: row for row in sources["executor_inventory"] if row["id"].startswith("hivemind.")}
    assert set(rows) == HIVEMIND_IDS
    assert {row["discovery_status"] for row in rows.values()} == {"discovered"}


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

    def with_unreviewed_label(repo_root, **kwargs):
        census = original(repo_root, **kwargs)
        census["current"]["source_labels"].add("hivemind.unreviewed")
        return census

    monkeypatch.setattr(capability_ledger, "_census_sets", with_unreviewed_label)
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    row = ledger["sources"]["coverage"]["source_labels"]
    assert row["status"] == "drift"
    assert row["added"] == ["hivemind.unreviewed"]
    assert row["complete"] is False


def test_hivemind_is_excluded_from_the_in_tree_source_label_census():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    assert not any(row["pack"] == "hivemind" for row in ledger["sources"]["pack_labels"])
    matrix_ids = {row["id"] for row in ledger["capabilities"]}
    assert HIVEMIND_IDS <= matrix_ids


def test_moved_pack_labels_are_not_reported_as_installed():
    from astrid.core.execution.capability_ledger import capability_execution_admission

    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    # capture, chiptune and pixel moved to poms_packs: their labels are absent
    # from a core-only source census, while their reviewed matrix rows remain.
    assert not [row for row in ledger["sources"]["pack_labels"] if row["pack"] in {"capture", "chiptune", "pixel"}]
    matrix = {row["id"]: row for row in ledger["capabilities"]}
    for action_id, resources in (("pixel.snap", ["cpu"]), ("chiptune.compose", ["cpu"]), ("capture.web_page", ["provider"])):
        assert matrix[action_id]["disposition"] == "optional"
        assert matrix[action_id]["resource_keys"] == resources
    # Reconciliation is complete against the core-only lock; the matrix row
    # admits the capability, which still needs runtime readiness to run.
    admission = capability_execution_admission(ledger, "pixel.snap")
    assert admission["executable"] is True
    assert admission["requires_runtime_readiness"] is True


def test_reviewed_v3_source_occurrence_removal_is_reported_by_name(monkeypatch):
    from astrid.core.execution import capability_ledger

    original = capability_ledger._census_sets

    def without_reviewed_occurrence(repo_root, **kwargs):
        census = original(repo_root, **kwargs)
        census["current"]["source_labels"].discard("generation.generate_audio")
        return census

    monkeypatch.setattr(capability_ledger, "_census_sets", without_reviewed_occurrence)
    row = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))["sources"]["coverage"]["source_labels"]
    assert row["status"] == "drift"
    assert "generation.generate_audio" in row["missing"]
