"""B9.1 source reconciliation and no-drop coverage."""

import json
from pathlib import Path

from astrid.core.execution.capability_ledger import load_capability_ledger


MOVED_PACKS_NOTE = (
    "Core-only install: packs that moved to poms_packs are in the approved census "
    "lock but not installed, so they are reported missing (drift) while nothing "
    "installed is unreviewed."
)
INSTALLED_PACKS = {
    "comfy_wrap", "generation", "h3_av", "local", "rendering",
    "runpod", "training", "vibecomfy", "wan2gp",
}


def test_shipped_ledger_reconciles_historical_capability_sets():
    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    sources = ledger["sources"]
    coverage = sources["coverage"]

    # Counts are the installed core packs only; the approved census lock still
    # carries the moved packs (110 / 111 labels).
    assert sources["counts"]["pack_labels"] == 42
    assert sources["counts"]["historical_pack_labels"] == 46
    assert sources["counts"]["executor_inventory"] == 56
    assert coverage["source_labels"]["approved"] == 110
    assert coverage["historical_source_labels"]["approved"] == 111

    # Drift is exactly "approved but not installed": nothing unreviewed in the
    # label censuses, and every missing id belongs to a pack that is not installed.
    # See MOVED_PACKS_NOTE.
    for section in ("source_labels", "historical_source_labels", "executor_inventory"):
        row = coverage[section]
        assert row["status"] == "drift" and row["complete"] is False
        assert row["missing"]
        assert not {name.split(".", 1)[0] for name in row["missing"]} & INSTALLED_PACKS
    assert coverage["source_labels"]["added"] == []
    assert coverage["historical_source_labels"]["added"] == []
    # The only executors installed but not yet in the approved lock.
    assert coverage["executor_inventory"]["added"] == [
        "runpod.ls", "runpod.release", "runpod.setup", "runpod.status",
    ]
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

    host = GenericPackHost(
        pack_roots=[Path("astrid/packs")],
        capability_matrix=Path("config/astrid-beta-capabilities.json"),
    )
    assert host.ledger["sources"]["counts"]["pack_labels"] == 42
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


def test_moved_pack_labels_are_not_reported_as_installed_and_admission_is_denied():
    from astrid.core.execution.capability_ledger import capability_execution_admission

    ledger = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))
    # capture, chiptune and pixel moved to poms_packs: their labels are absent
    # from a core-only source census, while their reviewed matrix rows remain.
    assert not [row for row in ledger["sources"]["pack_labels"] if row["pack"] in {"capture", "chiptune", "pixel"}]
    matrix = {row["id"]: row for row in ledger["capabilities"]}
    for action_id, resources in (("pixel.snap", ["cpu"]), ("chiptune.compose", ["cpu"]), ("capture.web_page", ["provider"])):
        assert matrix[action_id]["disposition"] == "optional"
        assert matrix[action_id]["resource_keys"] == resources
    # Reconciliation is incomplete against the unchanged approved census lock,
    # so admission is denied for every capability until the lock is re-approved.
    admission = capability_execution_admission(ledger, "pixel.snap")
    assert admission["executable"] is False
    assert admission["reason"] == "capability source reconciliation is incomplete"


def test_reviewed_v3_source_occurrence_removal_is_reported_by_name(monkeypatch):
    from astrid.core.execution import capability_ledger

    original = capability_ledger._census_sets

    def without_reviewed_occurrence(repo_root, **kwargs):
        census = original(repo_root, **kwargs)
        census["current"]["source_labels"].discard("h3_av.finalize_publication")
        return census

    monkeypatch.setattr(capability_ledger, "_census_sets", without_reviewed_occurrence)
    row = load_capability_ledger(Path("config/astrid-beta-capabilities.json"))["sources"]["coverage"]["source_labels"]
    assert row["status"] == "drift"
    assert "h3_av.finalize_publication" in row["missing"]
