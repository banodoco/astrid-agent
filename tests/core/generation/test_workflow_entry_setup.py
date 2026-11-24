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
    authority = preview_entry_authority(entry, authorized, "local")
    assert authorize_nodes([node], placement_class="local", entry=entry, retry_argv=retry,
                           preview_authority=authority) == authorized
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([dict(node, commit="b" * 40)], placement_class="local",
                        entry=entry, retry_argv=retry, preview_authority=authority)
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([node], placement_class="runpod", entry=entry, retry_argv=retry,
                        preview_authority=authority)
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([node], placement_class="local",
                        entry=dict(entry, bundle=str(tmp_path / "other")), retry_argv=retry,
                        preview_authority=authority)
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([node], placement_class="local", entry=entry,
                        retry_argv=retry, persist=True, preview_authority=authority)
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([node], placement_class="local", entry=entry, retry_argv=retry)
    assert not (tmp_path / "candidate").exists()


def test_explicit_preview_authority_cannot_add_to_real_saved_approval_sidecar(tmp_path, monkeypatch):
    import json

    import pytest
    from vibecomfy.entries import load_entry
    from astrid.core.generation.node_approvals import (
        APPROVALS_FILENAME, approval_record, authorize_nodes, consent_token,
        preview_entry_authority, read_entry_approvals, write_entry_approvals,
    )
    from astrid.core.generation.preflight import GenerationReadinessError

    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    directory = root / "mine"
    directory.mkdir(parents=True)
    (directory / "entry.json").write_text(json.dumps({
        "id": "mine", "kind": "image", "modes": ["t2i"], "bundle": "workflow.py",
        "binds": {}, "defaults": {}, "needs": [],
    }))
    entry = dict(load_entry(directory), source="user-entry")
    old = {"repo": "https://github.com/example/preview-only", "commit": "a" * 40}
    new = dict(old, commit="b" * 40)
    old_record = approval_record(old, placement_class="local")
    new_record = approval_record(new, placement_class="local")
    write_entry_approvals(directory, [old_record])
    before = (directory / APPROVALS_FILENAME).read_bytes()
    retry = ["astrid", "generate", "image", "--entry", "mine"]
    consent = ["code=" + consent_token(new_record)]
    authorized = authorize_nodes([new], placement_class="local", entry=entry,
                                 retry_argv=retry, consent=consent, persist=False)
    authority = preview_entry_authority(entry, authorized, "local")
    assert authorize_nodes([new], placement_class="local", entry=entry, retry_argv=retry,
                           preview_authority=authority) == authorized
    with pytest.raises(GenerationReadinessError, match="cannot persist"):
        authorize_nodes([new], placement_class="local", entry=entry, retry_argv=retry,
                        persist=True, consent=consent, preview_authority=authority)
    assert (directory / APPROVALS_FILENAME).read_bytes() == before
    assert read_entry_approvals(directory) == [old_record]
    with pytest.raises(GenerationReadinessError):
        authorize_nodes([new], placement_class="local", entry=entry, retry_argv=retry)


def test_target_admission_refuses_preview_authority_on_live_path(tmp_path):
    """Reject preview arguments before candidate discovery or target admission."""
    import pytest
    from astrid.core.generation.node_approvals import preview_entry_authority
    from astrid.sdk.generation import GenerationFacade, GenerationInputError

    candidate = {"id": "mine", "bundle": str(tmp_path / "candidate")}
    authority = preview_entry_authority(candidate, [], "local")
    with pytest.raises(GenerationInputError, match="only valid on an explicit dry run"):
        GenerationFacade()._generate_on_target_impl(
            _job_dirs=[], registry=None, model=None, mode="t2i", modality="image",
            placement="managed", execution=None, inputs={}, dry_run=False,
            workflow_entry=candidate, preview_authority=authority,
        )
