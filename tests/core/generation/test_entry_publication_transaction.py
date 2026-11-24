"""RS1.10: actual-node and approval-write refusal must leave saved bytes intact."""
import json
from pathlib import Path

import pytest

from astrid.core.generation import cli, node_approvals as approvals
from vibecomfy.entries import entries_dir, import_as_entry, load_entry

REPO = "https://example.test/retained-rs1-pack.git"
PIN = "a" * 40


def _source(tmp_path, *, name="source", ref=None):
    from vibecomfy.templates import OutputSpec, ReadyMetadata, new_workflow
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate

    with new_workflow(ReadyMetadata.build(template_id="rs1-entry-fixture", capability="image")) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        output = SaveImage(images=image.out("IMAGE"), filename_prefix=name)
        workflow.finalize({}, outputs=[OutputSpec(node=output)])
    if ref is not None:
        workflow.requirements.custom_node_refs.append({
            "name": "retained-rs1-pack", "slug": "retained-rs1-pack",
            "source": "git", "url": ref["repo"], "commit": ref["commit"],
        })
    return emit_bundle_with_candidate(workflow, tmp_path / (name + ".py"), Provenance.USER_CONFIRMED,
                                      None, source_format="scratchpad").python_path


def _tree(root):
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


@pytest.fixture
def entry_root(tmp_path, monkeypatch):
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save-only opened Runtime"))
    return root


def _argv(source, *, replace=False):
    return ["workflow", str(source), "--as-entry", "mine", "--no-run",
            "--placement", "managed", "--json", *(["--replace"] if replace else [])]


def test_retained_unapproved_node_ref_does_not_replace_entry(tmp_path, entry_root, capsys):
    from astrid.core.generation import entries
    from vibecomfy.entries import entry_added_node_refs

    source = _source(tmp_path)
    original = import_as_entry(source, "mine", add=[REPO + "@" + PIN])
    directory = entry_root / "mine"
    assert any(row["commit"] == PIN for row in entry_added_node_refs(original))
    old_nodes = entries.engine_table()["comfyui"].workflow_requirements(
        [entries._load_entry_workflow(original)])["extra"]["nodes"]
    assert any(row["repo"] == REPO and row["commit"] == PIN for row in old_nodes)
    approvals.write_entry_approvals(directory, [approvals.approval_record(
        row, placement_class="local") for row in old_nodes])
    before = _tree(directory)

    # The replacement source has no such declaration. The canonical import
    # retains the old explicit addition, so actual-node admission must refuse.
    assert cli.main(_argv(source, replace=True)) == 2
    error = json.loads(capsys.readouterr().out)
    assert "no matching code approval" in json.dumps(error)
    assert load_entry(directory) == original
    assert _tree(directory) == before


@pytest.mark.parametrize("replace", [False, True], ids=["new", "replacement"])
def test_approval_write_failure_does_not_publish(tmp_path, entry_root, monkeypatch, capsys, replace):
    source = _source(tmp_path)
    directory = entry_root / "mine"
    original = import_as_entry(source, "mine") if replace else None
    if replace:
        approvals.write_entry_approvals(directory, [])
    else:
        entry_root.mkdir()
    before = _tree(entry_root)

    def refuse_write(revision, records):
        assert Path(revision, "entry.json").is_file()
        raise OSError("approval write failed")

    monkeypatch.setattr(approvals, "write_entry_approvals", refuse_write)
    assert cli.main(_argv(source, replace=replace)) == 2
    assert "approval write failed" in capsys.readouterr().out
    assert _tree(entry_root) == before
    if replace:
        assert load_entry(directory) == original
    else:
        assert not directory.exists()


def test_successful_replacement_publishes_exact_revision_approval(tmp_path, entry_root, capsys):
    from astrid.core.generation import entries

    node_a = {"repo": REPO, "commit": PIN}
    node_b = {**node_a, "commit": "b" * 40}
    source_a = _source(tmp_path, name="first", ref=node_a)
    source_b = _source(tmp_path, name="second", ref=node_b)

    def save_with_consent(source, *, replace=False):
        assert cli.main(_argv(source, replace=replace)) == 2
        warning = json.loads(capsys.readouterr().out)
        assert warning["issues"][0]["code"] == "needs_consent"
        retry = warning["fixes"][0]["instruction"]["then_argv"]
        assert cli.main(retry[2:]) == 0
        assert json.loads(capsys.readouterr().out)["saved"] is True

    save_with_consent(source_a)
    original = load_entry(entry_root / "mine")
    previous = _tree(entry_root / "mine")
    save_with_consent(source_b, replace=True)
    published = entries.find_entry("mine")
    revision = Path(published["bundle"]).parent
    assert revision != Path(original["bundle"]).parent
    actual = entries.engine_table()["comfyui"].workflow_requirements(
        [entries._load_entry_workflow(published)])["extra"]["nodes"]
    assert any(row["commit"] == node_b["commit"] for row in actual)
    records = approvals.read_entry_approvals(revision)
    assert records == [approvals.approval_record(row, placement_class="local") for row in actual]
    for path, value in previous.items():
        if path != Path("entry.json") and value is not None:
            assert (entry_root / "mine" / path).read_bytes() == value
    # The ordinary saved-entry admission must select the same revision.
    assert approvals.entry_approval_directory(published) == revision
    assert approvals.authorize_nodes(actual, placement_class="local", entry=published,
                                     retry_argv=["astrid", "generate", "image"]) == records
