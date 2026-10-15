"""Bounded synthetic publication proofs; no live Runtime or named-film input.

The reused acceptance double supplies the exact closure port missing from the
pinned Runtime. History rows below match C HISTORY's public resource shape;
they deliberately omit labels/messages and retain repeated revision events.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli, cli_history
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import commands, inverse, saved_publish, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline.revisions import RuntimeRevisions, resolve_revision
from astrid.sdk.timeline.saved_intent import read_saved_calls
from astrid.sdk.timeline._common import TimelineEditError
from test_timeline_saved_publish import RuntimeFixture, edit


from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


class HistoryRuntime(RuntimeFixture):
    def __init__(self):
        super().__init__()
        self.history = [[self.event("rev-0", "pub-0", None, None, via="app")], None]
        self.restores = []
        self.summaries, self.evidence_store = [], {}

    @staticmethod
    def event(revision, publication, previous, previous_publication, *, via="python", restored=None):
        return dict(project_id="p", timeline_id="t", revision_id=revision, publication_id=publication,
                    previous_revision_id=previous, previous_publication_id=previous_publication,
                    restored_from_revision_id=restored, created_at="2026-10-15T00:00:00Z",
                    provenance=dict(source=dict(via=via)), is_current_head=True)

    def add_event(self, event):
        for row in self.history[0]:
            row["is_current_head"] = False
        self.history[0].insert(0, event)

    def port(self, body):
        port = super().port(body)
        accept = port.accept
        def accepted(prepared):
            previous = copy.deepcopy(self.history[0][0])
            result = accept(prepared)
            saved = result["stored_result"]
            if saved["status"] == "accepted":
                digest = saved["evidence"]["digest"]
                self.evidence_store[digest] = copy.deepcopy(prepared["evidence"])
                self.summaries.insert(0, dict(revision_id=saved["accepted_revision_id"],
                    publication_id=saved["accepted_publication_id"], previous_revision_id=previous["revision_id"],
                    previous_publication_id=previous["publication_id"], label=body.get("label"), evidence_digest=digest))
                self.add_event(self.event(saved["accepted_revision_id"], saved["accepted_publication_id"],
                                          previous["revision_id"], previous["publication_id"], via=body["source"]["via"]))
            return result
        port.accept = accepted
        port.head = lambda: dict(revision_id=self.current, publication_id=self.history[0][0]["publication_id"], updated_at="fixture")
        # Exact C provider summary/evidence APIs (no Runtime is imported).
        port.revisions = lambda options: self.summaries if not options.get("before") else []
        port.evidence = lambda digest: copy.deepcopy(self.evidence_store[digest])
        return port

    def transport(self, method, path, headers, payload):
        if path.endswith("/restore"):
            source = path.split("/")[-2]
            body = json.loads(payload)
            key = headers["Idempotency-Key"]
            previous = self.history[0][0]
            self.restores.append((source, body, key))
            assert set(body) == {"expected_head"} and body["expected_head"] == self.current
            # Runtime restore republishes the SAME historical revision ID.
            self.current = source
            self.add_event(self.event(source, "restore-publication", previous["revision_id"],
                                      previous["publication_id"], via="restore", restored=source))
            response = dict(data=dict(revision_id=source), receipt=dict(receipt_id="restore-publication"))
            return 200, {}, json.dumps(response).encode()
        return super().transport(method, path, headers, payload)

    def product(self):
        workspace = self.client()
        class Timelines:
            def list_parent_composition_revisions(self, project, timeline, **kwargs):
                return DomainResult.success(workspace.list_project_parent_composition_revisions(project, timeline, **kwargs))
            def head(self, project, timeline):
                return dict(revision_id=self_runtime.current)
            def restore_parent_composition_revision(self, project, timeline, revision, **kwargs):
                return DomainResult.success(workspace.restore_project_parent_composition_revision(project, timeline, revision, **kwargs))
        self_runtime = self
        return SimpleNamespace(_transport=workspace, timelines=Timelines())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(Checkout, "SAY_SAVED", False)
    runtime = HistoryRuntime()
    client = runtime.product()
    body = dict(project_id="p", timeline_id="t", base_revision_id="rev-0")
    monkeypatch.setattr(store, "resolve_ids", lambda *a, **kw: ("p", "t", runtime.current))
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, revision_id=None, **kw:
                        saved_publish._exact(client._transport, body, revision_id or runtime.current))
    return Checkout.draft("p", "t"), runtime, client


def publish(draft, client, message):
    result = draft.publish(message, client=client)
    assert result["status"] == "accepted" and result["local_state"] == "finalized"
    return result


def parsed(client, **kwargs):
    return SimpleNamespace(client=client, project="p", ref="t", limit=50, json=True, **kwargs)


def test_own_publication_undo_stages_guarded_commands_then_commits_on_moved_head(setup):
    draft, runtime, client = setup
    before = draft.clip("c1.rocket").params["x"]
    edit(draft, x=90)
    original = publish(draft, client, "move rocket")
    other = Checkout.draft("p", "t", name="other")
    edit(other, y=180)
    later = publish(other, client, "move y")
    later_y = other.clip("c1.rocket").params["y"]
    draft = Checkout.load(draft.path)
    assert draft.undo(client=client) == [f"Undo publication {original['accepted_publication_id']}"]
    staged = read_saved_calls(Checkout.load(draft.path))
    assert len(staged) == 1 and staged[0]["op"] == "timeline.inverse"
    assert staged[0]["expect"] and staged[0]["args"]["publication"] == original["accepted_publication_id"]
    result = publish(Checkout.load(draft.path), client, "Undo move rocket")
    assert result["previous_revision_id"] == later["accepted_revision_id"]
    assert result["rebased_onto"] == later["accepted_revision_id"]
    final = Checkout(result["canonical_document"]).clip("c1.rocket")
    assert final.params["x"] == before and final.params["y"] == later_y
    assert result["accepted_publication_id"] != original["accepted_publication_id"]
    assert json.loads(runtime.calls[-1][0])["label"] == "Undo move rocket"


def test_overlap_names_exact_later_publication_and_restore_without_losing_work(setup):
    draft, runtime, client = setup
    edit(draft, x=90)
    original = publish(draft, client, "first")
    other = Checkout.draft("p", "t", name="other")
    edit(other, x=150)
    later = publish(other, client, "later x")
    clean = Path(draft.path).read_bytes()
    with pytest.raises(TimelineEditError, match="same-property") as exc:
        draft.undo(client=client, publication=original["accepted_publication_id"])
    assert later["accepted_publication_id"] in str(exc.value) and "later x" in str(exc.value)
    assert "timelines restore" in str(exc.value)
    assert Path(draft.path).read_bytes() == clean and runtime.current == later["accepted_revision_id"]


def test_restore_uses_generated_contract_and_history_records_source_revision(setup, capsys):
    draft, runtime, client = setup
    edit(draft, x=90)
    first = publish(draft, client, "first message")
    edit(draft, y=180)
    second = publish(draft, client, "second message")
    store_ = RuntimeRevisions(client.timelines)
    assert resolve_revision("p", "t", "prev", store=store_) == first["accepted_revision_id"]
    assert resolve_revision("p", "t", "-2", store=store_) == "rev-0"
    assert cli_history.run_restore(parsed(client, from_ref="@prev", idempotency_key="restore-key")) == 0
    capsys.readouterr()
    assert runtime.restores == [(first["accepted_revision_id"], {"expected_head": second["accepted_revision_id"]}, "restore-key")]
    events = store_.list("p", "t")
    assert events[0].source == "restore" and events[0].id == events[2].id
    assert events[0].publication_id != events[2].publication_id
    assert events[0].restored_from_revision_id == first["accepted_revision_id"]
    assert resolve_revision("p", "t", "prev", store=store_) == second["accepted_revision_id"]
    assert resolve_revision("p", "t", "-2", store=store_) == first["accepted_revision_id"]
    cli_history.run_history(parsed(client))
    rows = json.loads(capsys.readouterr().out)["data"]["revisions"]
    assert rows[0]["source"] == "restore" and rows[0]["message"] is None
    assert rows[0]["restored_from_revision_id"] == first["accepted_revision_id"]
    assert rows[1]["source"] == "python" and rows[1]["message"] == "second message"
    assert rows[1]["created_at"] == "2026-10-15T00:00:00Z"
    assert rows[1]["provenance"] == {"source": {"via": "python"}}


@pytest.mark.parametrize("verb", ["undo", "revert"])
def test_undo_and_revert_are_one_cli_handler_and_restore_is_only_new_verb(setup, monkeypatch, verb):
    draft, runtime, client = setup
    edit(draft, x=90)
    publication = publish(draft, client, "first")["accepted_publication_id"]
    undo = next(spec for spec in cli.COMMANDS if spec.name == "undo")
    assert undo.aliases == ("revert",)
    assert not any(spec.name == "revert" for spec in cli.COMMANDS)
    parser = cli.build_parser(client)
    args = parser.parse_args([verb, "t", "--project", "p", publication])
    monkeypatch.setattr(cli, "_edit_checkout", lambda parsed: Checkout.load(draft.path))
    assert args.handler(args) == 0
    assert read_saved_calls(Checkout.load(draft.path))[0]["args"]["publication"] == publication


def test_undo_and_revert_have_byte_equivalent_cli_results(monkeypatch, capsys):
    class CheckoutFake:
        def undo(self, steps, *, client, publication):
            return [f"Undo publication {publication} in {steps} step(s)"]

    parser = cli.build_parser(object())
    output_bytes = []
    monkeypatch.setattr(cli, "_edit_checkout", lambda _parsed: CheckoutFake())

    for verb in ("undo", "revert"):
        args = parser.parse_args([verb, "t", "--project", "p", "pub-1"])
        assert args.handler(args) == 0
        output_bytes.append(capsys.readouterr().out.encode("utf-8"))

    assert output_bytes[0] == output_bytes[1]


def test_presence_repeated_writes_and_consequences_are_inverted_through_runner(setup):
    draft, runtime, client = setup
    before = copy.deepcopy(draft.clip("c1.rocket").params)
    edit(draft, x=90)
    edit(draft, x=120)
    publish(draft, client, "two x writes")
    draft.undo(client=client)
    assert draft.clip("c1.rocket").params == before
    publish(draft, client, "undo two x writes")


def test_fake_history_refuses_unrelated_links_snapshots_timestamps_and_prefixes(setup):
    _draft, runtime, client = setup
    runtime.history = [[dict(revision_id="restore-new", publication_id="p2", previous_revision_id=None,
        previous_publication_id=None, parent_revision_id="rev-0", created_at="2099", is_current_head=True,
        snapshot={"previous_revision_id": "rev-0"}),
        dict(revision_id="rev-0", publication_id="p1", created_at="2000", is_current_head=False)], None]
    store_ = RuntimeRevisions(client.timelines)
    with pytest.raises(TimelineEditError, match="ancestry is unavailable"):
        resolve_revision("p", "t", "prev", store=store_)
    with pytest.raises(TimelineEditError, match="no explicit ancestry"):
        inverse.publication_chain(store_.list("p", "t"), "p1")


def test_overlap_after_staging_is_refused_by_commit_and_names_current_publication(setup):
    draft, runtime, client = setup
    edit(draft, x=90)
    publish(draft, client, "original")
    draft.undo(client=client)
    other = Checkout.draft("p", "t", name="other")
    edit(other, x=150)
    later = publish(other, client, "later overlap")
    staged = Path(draft.path).read_bytes()
    result = draft.publish("Undo original", client=client)
    assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "same-property"
    assert later["accepted_publication_id"] in result["choices"][0]["options"][0]["label"]
    assert "timelines restore" in result["choices"][0]["options"][0]["label"]
    assert runtime.current == later["accepted_revision_id"] and Path(draft.path).read_bytes() == staged


@pytest.mark.parametrize("after_staging", [False, True])
def test_aba_overlap_uses_publication_evidence_even_when_final_value_matches(setup, after_staging):
    draft, runtime, client = setup
    edit(draft, x=90)
    publish(draft, client, "original")
    if after_staging:
        draft.undo(client=client)
    other = Checkout.draft("p", "t", name="other")
    edit(other, x=150)
    publish(other, client, "away")
    edit(other, x=90)
    later = publish(other, client, "back")
    if after_staging:
        result = draft.publish("Undo original", client=client)
        assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "same-property"
        assert later["accepted_publication_id"] in result["choices"][0]["options"][0]["label"]
    else:
        with pytest.raises(TimelineEditError, match=later["accepted_publication_id"]):
            draft.undo(client=client)
    assert runtime.current == later["accepted_revision_id"]


def test_later_restore_overlap_names_restore_and_its_distinct_publication(setup, capsys):
    draft, runtime, client = setup
    edit(draft, x=90)
    publish(draft, client, "original")
    assert cli_history.run_restore(parsed(client, from_ref="rev-0", idempotency_key="restore-key")) == 0
    with pytest.raises(TimelineEditError, match="same-property") as exc:
        draft.undo(client=client)
    assert "restore-publication (restore)" in str(exc.value)
    assert runtime.current == "rev-0"


def test_property_inverse_preserves_absence_null_and_opaque_clear_objects(setup):
    draft, _runtime, _client = setup
    sid = "occurrence:occ-A/clip:a-rocket"
    base = draft.document()
    for old in ({"state": "absent"}, {"state": "present", "value": None},
                {"state": "present", "value": {"op": "clear"}}):
        before = Checkout(copy.deepcopy(base))
        if old["state"] == "present":
            before.clip("c1.rocket").params["opaque"] = copy.deepcopy(old["value"])
        after = Checkout(before.document())
        after.clip("c1.rocket").params["opaque"] = "new"
        call = dict(op="clip.set", select=sid, args=dict(opaque="new"), resolved=[sid],
                    expect={"params.opaque": old}, bindings={}, created=[], authorship={"params.opaque": "direct"})
        command = inverse.inverse_command([call], before.document(), after.document(), "pub-test")
        document, plan, _ = RuntimeCommitService._apply(after.document(), [command])
        state = commands.state_at(commands.entity_states(Checkout(document)), sid, "params.opaque")
        assert state == old and plan.calls[0].expect["params.opaque"] == {"state": "present", "value": "new"}


def test_public_history_pages_until_ancestry_is_present_and_never_uses_adjacency():
    class Paged:
        def list_parent_composition_revisions(self, project, timeline, *, cursor, limit):
            if cursor is None:
                return dict(items=[dict(revision_id="same", publication_id="p3", previous_revision_id="same",
                    previous_publication_id="p2", is_current_head=True),
                    dict(revision_id="unrelated", publication_id="other", is_current_head=False)], next_cursor="next")
            return dict(items=[dict(revision_id="same", publication_id="p2", previous_revision_id="old",
                previous_publication_id="p1", is_current_head=False),
                dict(revision_id="old", publication_id="p1", is_current_head=False)], next_cursor=None)
    store_ = RuntimeRevisions(Paged())
    assert resolve_revision("p", "t", "prev", store=store_) == "same"
    assert resolve_revision("p", "t", "-2", store=store_) == "old"
    class Migrated:
        def list_parent_composition_revisions(self, *args, **kwargs):
            return dict(items=[dict(revision_id="new", is_current_head=True, parent_revision_id="old"),
                               dict(revision_id="old", is_current_head=False)], next_cursor=None)
    with pytest.raises(TimelineEditError, match="revision ancestry is unavailable"):
        resolve_revision("p", "t", "prev", store=RuntimeRevisions(Migrated()))


def test_same_revision_unproven_publication_race_fails_closed_without_inventing_history(setup):
    draft, runtime, client = setup
    edit(draft, x=90)
    original = publish(draft, client, "original")
    draft.undo(client=client)
    runtime.add_event(runtime.event(runtime.current, "another-publication-of-same-revision", runtime.current,
                                    original["accepted_publication_id"], via="app"))
    before = len(runtime.publications)
    result = draft.publish("Undo original", client=client)
    assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "dependent"
    assert "another-publication-of-same-revision" in result["choices"][0]["options"][0]["label"]
    assert len(runtime.publications) == before and runtime.current == original["accepted_revision_id"]


def test_commit_conflict_attributes_actual_writer_even_with_a_newer_disjoint_publication(setup):
    draft, runtime, client = setup
    edit(draft, x=90)
    publish(draft, client, "original")
    draft.undo(client=client)
    other = Checkout.draft("p", "t", name="other")
    edit(other, x=150)
    overlap = publish(other, client, "writer of x")
    edit(other, y=180)
    newest = publish(other, client, "unrelated y")
    result = draft.publish("Undo original", client=client)
    assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "same-property"
    label = result["choices"][0]["options"][0]["label"]
    assert overlap["accepted_publication_id"] in label and "writer of x" in label
    assert newest["accepted_publication_id"] not in label and runtime.current == newest["accepted_revision_id"]


def test_inverse_identity_map_rewrites_targets_and_preserves_opaque_publication_text(setup):
    from astrid.sdk.timeline.rebase import rebase
    draft, _runtime, _client = setup
    before = draft.document()
    after, forward, _ = RuntimeCommitService._apply(before, [dict(op="clip.set", select="c1.rocket", args=dict(x=120))])
    sid, renamed = "occurrence:occ-A/clip:a-rocket", "occurrence:occ-A/clip:renamed"
    value = inverse.inverse_command(forward.calls, before, after, sid)
    _, backward, _ = RuntimeCommitService._apply(after, [value])
    head = Checkout(copy.deepcopy(after))
    head.clip("a-rocket").data["id"] = "renamed"
    result = rebase(backward.calls, head.document(), id_map={sid: renamed})
    assert not result.conflicts and result.id_map == {sid: renamed}
    assert result.plan.calls[0].args["writes"][0]["id"] == renamed
    assert result.plan.calls[0].args["publication"] == sid
    assert Checkout(result.document).clip("renamed").params["x"] == Checkout(before).clip("a-rocket").params["x"]
    missing = rebase(backward.calls, head.document())
    assert missing.conflicts[0]["kind"] == "delete-vs-edit" and missing.document is None


def test_aliases_keep_numeric_edit_counts_and_reject_zero():
    for verb in ("undo", "revert"):
        parser = cli.build_parser(None)
        assert parser.parse_args([verb, "t", "2", "--project", "p"]).steps == 2
        assert parser.parse_args([verb, "t", "--project", "p", "2"]).steps == 2
        assert parser.parse_args([verb, "t", "--steps", "2", "--project", "p"]).steps_option == 2
        with pytest.raises(SystemExit):
            parser.parse_args([verb, "t", "0", "--project", "p"])


def test_removed_entity_inverse_restores_recorded_identity_without_snapshot_replacement(setup):
    draft, runtime, client = setup
    original = copy.deepcopy(draft.clip("a-rocket").data)
    commands.run(draft, dict(op="clip.remove", select="a-rocket", args={}))
    result = publish(draft, client, "remove a layer")
    draft.undo(client=client)
    restored = publish(draft, client, "restore removed layer by inverse")
    assert draft.clip("a-rocket").data == original
    assert restored["accepted_publication_id"] != result["accepted_publication_id"]


def test_explicit_unknown_provenance_never_becomes_a_revision_prefix_source():
    class Migrated:
        def list_parent_composition_revisions(self, *args, **kwargs):
            return dict(items=[dict(revision_id="restore-old-content-name", publication_id=None,
                                   provenance=None, is_current_head=True)], next_cursor=None)
    event = RuntimeRevisions(Migrated()).list("p", "t")[0]
    assert event.source == "other" and event.provenance is None
