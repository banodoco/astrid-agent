"""LE-12 independent entry error and old approval preservation witnesses."""
from __future__ import annotations

from astrid.core.generation.workflow_entry_setup import entry_exists_error, retained_entry_approvals


def test_collision_fix_offers_explicit_replacement_and_drops_dry_run():
    argv = ["astrid", "generate", "workflow", "source.json", "--as-entry", "example",
            "--placement", "runpod", "--add", "https://github.com/example/node-pack@main",
            "--no-run", "--dry-run", "--consent=code=0123456789ab"]
    result = entry_exists_error(
        FileExistsError("entry already exists"), entry_id="example", retry_argv=argv,
    ).to_envelope()
    assert result["status"] == "blocked"
    assert result["entry"] == {"id": "example"}
    assert result["issues"][0]["code"] == "entry_exists"
    fix = result["fixes"][0]
    assert result["issues"][0]["fix_ids"] == [fix["id"]]
    assert fix["who"] == "human"
    assert fix["instruction"]["then_argv"] == [
        "astrid", "generate", "workflow", "source.json", "--as-entry", "example",
        "--placement", "runpod", "--add", "https://github.com/example/node-pack@main",
        "--no-run", "--replace", "--json",
    ]


def test_replacement_retains_old_pin_and_other_placement_records_without_granting_new_consent():
    from astrid.core.generation.node_approvals import approval_record, approval_matches

    old_node = {"repo": "https://github.com/example/node-pack", "commit": "a" * 40}
    new_node = {**old_node, "commit": "b" * 40}
    old = approval_record(old_node, placement_class="local")
    other = approval_record(old_node, placement_class="runpod")
    saved = [old, other]
    assert retained_entry_approvals(saved, [old]) == saved
    assert not any(approval_matches(record, new_node, placement_class="local") for record in saved)
    # A separate, freshly authorized record may be appended only after the
    # consent boundary accepts it. Retaining history must not overwrite it.
    new = approval_record(new_node, placement_class="local")
    merged = retained_entry_approvals(saved, [new])
    assert merged == [old, other, new]
    assert saved == [old, other]
    assert [record for record in merged if approval_matches(
        record, new_node, placement_class="local")] == [new]


def test_preview_authority_only_reuses_exact_already_authorized_records(tmp_path):
    import pytest

    from astrid.core.generation.node_approvals import (
        approval_record, authorize_nodes, consent_token, preview_entry_authority,
    )
    from astrid.core.generation.preflight import GenerationReadinessError

    node = {"repo": "https://github.com/example/preview-only", "commit": "a" * 40}
    retry = ["astrid", "generate", "workflow", "source.json", "--as-entry", "example"]
    record = approval_record(node, placement_class="local")
    authorized = authorize_nodes([node], placement_class="local",
                                 consent=["code=" + consent_token(record)], retry_argv=retry)
    entry = {"id": "example", "bundle": str(tmp_path / "candidate")}
    with preview_entry_authority(entry, authorized, "local"):
        assert authorize_nodes([node], placement_class="local", entry=entry, retry_argv=retry) == authorized
        with pytest.raises(GenerationReadinessError):
            authorize_nodes([dict(node, commit="b" * 40)], placement_class="local",
                            entry=entry, retry_argv=retry)
        with pytest.raises(GenerationReadinessError):
            authorize_nodes([node], placement_class="runpod", entry=entry, retry_argv=retry)
        with pytest.raises(GenerationReadinessError):
            authorize_nodes([node], placement_class="local",
                            entry=dict(entry, bundle=str(tmp_path / "other")), retry_argv=retry)
        with pytest.raises(GenerationReadinessError):
            authorize_nodes([node], placement_class="local", entry=entry,
                            retry_argv=retry, persist=True)
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([node], placement_class="local", entry=entry, retry_argv=retry)
    assert not (tmp_path / "candidate").exists()
