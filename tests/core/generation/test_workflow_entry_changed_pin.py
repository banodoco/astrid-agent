"""LE-12 real CLI replacement: new pins require fresh consent, old records stay."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation import cli

URL = "https://github.com/example/astrid-le12-unreviewed"
OLD = "a" * 40
NEW = "b" * 40


def test_replace_changed_add_commit_needs_fresh_consent_and_retains_previous_records(
    tmp_path, monkeypatch, capsys,
):
    from astrid.core.generation import node_approvals as approvals
    from vibecomfy.entries import entry_added_node_refs, load_entry
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.templates import OutputSpec, ReadyMetadata, new_workflow
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate

    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save-only opened Runtime"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("save-only ran the workflow"))
    with new_workflow(ReadyMetadata.build(template_id="le12-fixture", capability="image")) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        output = SaveImage(images=image.out("IMAGE"), filename_prefix="le12")
        workflow.finalize({}, outputs=[OutputSpec(node=output)])
    emitted = emit_bundle_with_candidate(workflow, tmp_path / "workflow.py",
                                         Provenance.USER_CONFIRMED, None, source_format="scratchpad")
    command = ["workflow", str(emitted.python_path), "--as-entry", "mine",
               "--placement", "managed", "--no-run", "--json"]
    destination = root / "mine"

    def refuse(argv):
        assert cli.main(argv) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["issues"][0]["code"] == "needs_consent", payload
        return payload["fixes"][0]["instruction"]["then_argv"]

    first_retry = refuse([*command, "--add", URL + "@" + OLD])
    assert not destination.exists()
    assert first_retry[:2] == ["astrid", "generate"]
    assert cli.main(first_retry[2:]) == 0
    assert json.loads(capsys.readouterr().out)["saved"] is True
    entry = load_entry(destination)
    old_records = approvals.read_entry_approvals(destination)
    assert any(row["identity"] == {"repo": URL, "commit": OLD} for row in old_records)
    old_bytes = (destination / approvals.APPROVALS_FILENAME).read_bytes()
    old_manifest = (destination / "entry.json").read_bytes()
    old_bundle = Path(entry["bundle"], "workflow.py").read_bytes()

    # Implicit replacement is a typed refusal even in preview, with the
    # exact repair asking for explicit replacement instead of deletion.
    assert cli.main([*command, "--add", URL + "@" + NEW, "--dry-run"]) == 2
    collision = json.loads(capsys.readouterr().out)
    assert collision["issues"][0]["code"] == "entry_exists"
    collision_retry = collision["fixes"][0]["instruction"]["then_argv"]
    assert "--replace" in collision_retry and "--dry-run" not in collision_retry
    assert (destination / approvals.APPROVALS_FILENAME).read_bytes() == old_bytes
    assert (destination / "entry.json").read_bytes() == old_manifest

    preview_retry = refuse([*command, "--replace", "--add", URL + "@" + NEW, "--dry-run"])
    assert "--dry-run" not in preview_retry
    assert (destination / approvals.APPROVALS_FILENAME).read_bytes() == old_bytes
    assert (destination / "entry.json").read_bytes() == old_manifest

    new_retry = refuse([*command, "--replace", "--add", URL + "@" + NEW])
    assert new_retry[:2] == ["astrid", "generate"]
    assert URL + "@" + NEW in new_retry
    assert preview_retry == new_retry
    assert (destination / approvals.APPROVALS_FILENAME).read_bytes() == old_bytes
    assert (destination / "entry.json").read_bytes() == old_manifest

    assert cli.main(new_retry[2:]) == 0
    assert json.loads(capsys.readouterr().out)["saved"] is True
    replaced = load_entry(destination)
    records = approvals.read_entry_approvals(destination)
    assert all(record in records for record in old_records)
    assert any(row["identity"] == {"repo": URL, "commit": NEW} for row in records)
    assert {(row["url"], row["commit"]) for row in entry_added_node_refs(replaced)} == {(URL, NEW)}
    assert Path(entry["bundle"], "workflow.py").read_bytes() == old_bundle
