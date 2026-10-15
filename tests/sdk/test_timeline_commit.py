"""Focused provider proofs; no Runtime/store/network or historical-film input.

CoreService substitutes only the closure read with a complete repository fixture.
The exact-port tests use the admitted detached shape. Neither proves an installed
Runtime/provider integration.
"""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.sdk.timeline import commands, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest, create_runtime_commit_service


def request(*calls, dry_run=False, timeline="t"):
    return dict(api_version=1, project_id="p", timeline_id=timeline, request_id="request-1",
                session_id="session-1", session_sequence=1, base_revision_id="rev-0",
                base_publication_id="publication-0", scope=dict(target="parent", complete=[], projection_version=1),
                change=dict(commands=list(calls)), source=dict(via="python"), dry_run=dry_run)


def call(op="clip.set", select="a-rocket", **args):
    return dict(op=op, select=select, args=args)


class StaleHead(Exception):
    code = "conflict"
    details = {"code": "StaleHead", "current_head_revision_id": "head-new"}


class Missing(Exception):
    code = "not_found"


class Port:
    """Request-bound acceptance double; zero persistence or authority inference."""
    def __init__(self, body, bundles=None):
        self.body = copy.deepcopy(body)
        self.bundles = bundles or {}
        self.current = "rev-0"
        self.saved = None
        self.accepts = []
        self.reads = []
        self.on_accept = None
        self.payload = dict(config={}, clips=[], occurrences=[])

    def lookup_request(self):
        return copy.deepcopy(self.saved)

    def head(self):
        return dict(revision_id=self.current, publication_id="pub-" + self.current, updated_at="fixture")

    def revision(self, revision_id):
        self.reads.append(revision_id)
        return dict(revision_id=revision_id, project_id=self.body["project_id"],
                    timeline_id=self.body["timeline_id"], payload=copy.deepcopy(self.payload),
                    content_digest=_digest(self.payload), created_at="fixture")

    def revision_closure(self, revision_id):
        return dict(parent_revision=self.revision(revision_id), shot_revisions=[], internal_timeline_revisions=[])

    def accept(self, prepared):
        self.accepts.append(copy.deepcopy(prepared))
        assert prepared["request_hash"] == _digest(self.body)
        assert prepared["project_id"] == self.body["project_id"]
        assert prepared["timeline_id"] == self.body["timeline_id"]
        assert prepared["request_id"] == self.body["request_id"]
        assert prepared["expected_head_revision_id"] == self.current
        for k in ("source", "asserted_actor", "label", "reason"):
            assert prepared.get(k) == self.body.get(k)
        assert not self.body["dry_run"]
        if self.on_accept:
            self.on_accept(self, prepared)
        result = copy.deepcopy(prepared["result"])
        if prepared["publication"] is not None:
            result.update(accepted_revision_id="accepted-fixture", accepted_publication_id="publication-fixture")
        receipt = dict(publisher="fixture-publisher", accepted_at="2026-10-15T00:00:00Z",
                       evidence_digest=_digest(prepared["evidence"]), narration_state=None)
        result.update(previous_revision_id=self.current, runtime_receipt=receipt,
                      provenance=dict(publisher="fixture-publisher", source=self.body["source"]),
                      evidence=dict(digest=receipt["evidence_digest"]))
        self.saved = dict(request_hash=prepared["request_hash"], result=result)
        return dict(stored_result=result)


class CoreService(RuntimeCommitService):
    @staticmethod
    def _load_revision(port, body, revision_id):
        # Explicit missing-producer substitution, not a claimed port API.
        port.revision(revision_id)
        return copy.deepcopy(port.bundles[revision_id])


@pytest.fixture
def bundle():
    path = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"
    return Checkout(json.loads(path.read_text())).document()


def core(body, bundle, head=None):
    port = Port(body, {"rev-0": bundle})
    if head is not None:
        port.current = "head-1"
        port.bundles["head-1"] = head
    return CoreService(), port


def edited(bundle, *calls):
    return CoreService._apply(bundle, calls)[0]


def test_factory_is_fixed_and_has_no_startup_io(monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("startup file I/O"))
    service = create_runtime_commit_service()
    assert service.provider_id == "astrid-v1"
    assert type(service.interface_version) is int and service.interface_version == 1
    assert service.contract_digest == "sha256:cd87ac6ee5d4d2ee1723b0eec9fe19145136a0daef4fffa06c174a4df5f6f6cd"
    assert callable(service.commit)


def test_current_port_exact_base_real_runner_noop_is_durably_recorded():
    body = request(call("timeline.resolve", None))
    port = Port(body)
    # resolve() materializes legacy missing slots on this unchanged baseline.
    # Use an already canonical revision for the actual no-op witness.
    port.payload["config"] = {"app": {"astrid": {"slots": {}}}}
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "no_change"
    assert port.reads == ["rev-0"] and len(port.accepts) == 1
    assert port.accepts[0]["publication"] is None
    assert result == port.saved["result"]
    assert result["accepted_revision_id"] is None


def test_current_port_refuses_missing_pinned_children():
    body = request(call(x=240))
    port = Port(body)
    port.payload["occurrences"] = [{"shot_id": "A", "shot_revision_id": "exact-child"}]
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "refused" and "closure" in result["error"]["message"]
    assert not port.accepts


def test_current_port_verifies_immutable_payload_digest():
    body = request()
    port = Port(body)
    revision = port.revision
    port.revision = lambda rid: {**revision(rid), "content_digest": "sha256:" + "0" * 64}
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "refused" and not port.accepts


def test_core_exact_base_uses_actual_recorder_compiler_and_direct_inventory(bundle, monkeypatch):
    body = request(call(select="c1.rocket", x=180), call(select="c1.rocket", x=240))
    original = copy.deepcopy(body), copy.deepcopy(bundle)
    service, port = core(body, bundle)
    invoked = []
    resolve = Checkout.resolve
    def observed(self):
        invoked.append(self.path)
        return resolve(self)
    monkeypatch.setattr(Checkout, "resolve", observed)
    monkeypatch.setattr(store, "publish_bundle", lambda *a, **k: pytest.fail("second write path"))
    monkeypatch.setattr(Checkout, "save", lambda *a, **k: pytest.fail("local draft write"))
    result = service.commit(body, port)
    assert result["status"] == "accepted" and result == port.saved["result"]
    assert invoked and set(invoked) == {None}
    assert body == original[0] and bundle == original[1]
    recorded = result["change"]["commands"]
    assert len(recorded) == 2 and recorded[0]["select"] == "a-rocket"
    assert recorded[0]["version"] and recorded[0]["resolved"] == ["occurrence:occ-A/clip:a-rocket"]
    assert recorded != body["change"]["commands"]
    assert {w["path"] for w in result["change"]["direct_writes"]} == {"params.x"}
    assert Checkout(result["canonical_document"]).clip("a-rocket").get("x") == 240
    publication = port.accepts[0]["publication"]
    assert publication == {k: compile_authoring_candidate(result["canonical_document"]).publication[k]
                           for k in publication}
    assert port.accepts[0]["evidence"]["commands"] == recorded
    assert port.accepts[0]["evidence"]["digests"]["after"] == result["document_digest"]


def test_core_no_change_does_not_publish(bundle):
    body = request(call(x=Checkout(copy.deepcopy(bundle)).clip("a-rocket").get("x")))
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "no_change" and port.accepts[0]["publication"] is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_core_stale_disjoint_clips_use_command_rebase(bundle, monkeypatch, dry_run):
    head = edited(bundle, call(select="b-type", text="head title"))
    body = request(call(x=240), dry_run=dry_run)
    service, port = core(body, bundle, head)
    monkeypatch.setattr(store, "three_way", lambda *a, **k: pytest.fail("document merge used for semantic calls"))
    result = service.commit(body, port)
    assert result["status"] == ("preview" if dry_run else "accepted")
    assert result["rebased_onto"] == "head-1"
    tl = Checkout(result["canonical_document"])
    assert tl.clip("a-rocket").get("x") == 240 and tl.clip("b-type").get("text") == "head title"
    assert len(port.accepts) == (0 if dry_run else 1)


@pytest.mark.parametrize("dry_run", [False, True])
def test_core_same_property_conflict_never_accepts(bundle, dry_run):
    head = edited(bundle, call(x=44))
    body = request(call(x=240), dry_run=dry_run)
    service, port = core(body, bundle, head)
    result = service.commit(body, port)
    assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "same-property"
    assert not port.accepts and result["runtime_receipt"] is None


def test_core_command_rebase_preserves_different_properties_of_one_clip(bundle):
    head = edited(bundle, call(y=48))
    body = request(call(x=240))
    service, port = core(body, bundle, head)
    result = service.commit(body, port)
    assert result["status"] == "accepted" and len(port.accepts) == 1
    clip = Checkout(result["canonical_document"]).clip("a-rocket")
    assert clip.get("x") == 240 and clip.get("y") == 48


@pytest.mark.parametrize("stales", [1, 2, 3])
def test_core_stalehead_bound_preserves_identity_and_applies_relative_command_once(bundle, stales):
    body = request(call("clip.nudge", seconds=0.1))
    service, port = core(body, bundle)
    expected = edited(bundle, body["change"]["commands"][0])
    def race(p, prepared):
        if len(p.accepts) <= stales:
            p.current = "race-" + str(len(p.accepts))
            p.bundles[p.current] = copy.deepcopy(bundle)
            raise StaleHead()
    port.on_accept = race
    result = service.commit(body, port)
    assert len(port.accepts) == min(stales + 1, 3)
    assert {p["request_id"] for p in port.accepts} == {body["request_id"]}
    assert {p["request_hash"] for p in port.accepts} == {_digest(body)}
    if stales == 3:
        assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "dependent"
        assert port.saved is None and result["accepted_revision_id"] is None
    else:
        assert result["status"] == "accepted"
        assert Checkout(result["canonical_document"]).clip("a-rocket").start == Checkout(expected).clip("a-rocket").start


def test_replay_precedes_base_read_even_after_head_moves_and_changed_body_conflicts(bundle):
    body = request(call(x=240))
    service, port = core(body, bundle)
    first = service.commit(body, port)
    port.current = "unreadable-new-head"
    port.bundles.clear()
    reads = list(port.reads)
    assert service.commit(body, port) == first
    changed = copy.deepcopy(body)
    changed["change"]["commands"][0]["args"]["x"] = 987
    result = service.commit(changed, port)
    assert result["error"]["code"] == "request_id_conflict"
    assert port.reads == reads and len(port.accepts) == 1


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
def test_unknown_acceptance_outcome_preserves_transport_and_replays_original(bundle, failure):
    body = request(call(x=240))
    service, port = core(body, bundle)
    accept = port.accept
    def lose_response(prepared):
        accept(prepared)
        raise failure("lost response")
    port.accept = lose_response
    with pytest.raises(failure):
        service.commit(body, port)
    port.current = "head-moved"
    assert service.commit(body, port) == port.saved["result"]
    assert len(port.accepts) == 1


def test_dry_run_does_not_lookup_reserve_or_accept():
    body = request(dry_run=True)
    port = Port(body)
    port.lookup_request = lambda: pytest.fail("dry-run replay lookup")
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "preview" and port.saved is None and not port.accepts


def test_future_feature_version_is_typed_before_opening_closure():
    body = request()
    port = Port(body)
    port.payload["config"] = {"app": {"astrid": {"v": 2, "opaque": {"kept": True}}}}
    before = copy.deepcopy(port.payload)
    result = create_runtime_commit_service().commit(body, port)
    assert result["error"]["code"] == "unsupported_feature" and not port.accepts
    assert port.payload == before


def test_missing_base_is_distinct_from_authorization_and_wrong_scope():
    body = request()
    port = Port(body)
    def missing(revision_id):
        raise Missing()
    port.revision = missing
    result = create_runtime_commit_service().commit(body, port)
    assert result["error"]["code"] == "base_not_found"
    def denied(revision_id):
        raise PermissionError("fixture authority")
    port.revision = denied
    with pytest.raises(PermissionError):
        create_runtime_commit_service().commit(body, port)


def test_mismatched_revision_identity_is_refused():
    body = request()
    port = Port(body)
    revision = port.revision
    port.revision = lambda rid: {**revision(rid), "timeline_id": "other"}
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "refused" and not port.accepts


@pytest.mark.parametrize("change", [dict(document={}, adapter="external"), dict(commands=[call("timeline.save", None)]),
                                     dict(commands=[call("timeline.add", None, element="image", at=0)])])
def test_missing_adapters_and_storage_or_acquisition_are_explicit_refusals(change):
    body = request()
    body["change"] = change
    port = Port(body)
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "refused" and not port.accepts


def test_core_clear_is_omission_after_exact_document_reload_without_absent_sentinel(bundle):
    calls = [call("clip.clear", keys=["x"])]
    after, plan, direct = CoreService._apply(bundle, calls)
    assert "x" not in Checkout(after).clip("a-rocket").params
    assert plan.calls and direct
    body = request(*calls)
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "accepted"
    reloaded = Checkout(json.loads(json.dumps(result["canonical_document"])))
    assert "x" not in reloaded.clip("a-rocket").params
    writes = result["change"]["direct_writes"]
    assert writes == [dict(id="occurrence:occ-A/clip:a-rocket", path="params.x",
                           before={"state": "present", "value": 10}, after={"state": "absent"})]
    assert result["change"]["commands"][0]["op"] == "clip.clear"


def test_core_batch_failure_does_not_mutate_source_or_accept(bundle):
    original = copy.deepcopy(bundle)
    body = request(call(x=240), call(unknown_parameter=55))
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result["status"] == "refused" and bundle == original and not port.accepts


@pytest.mark.parametrize("value", [call("timeline.adopt", None, timeline="other"),
                                  call("clip.set-beats", source="/not-admitted.json"),
                                  call("sequence.fit", "seq", shape_from="other@revision")])
def test_core_ambient_reads_are_refused_before_any_handler(bundle, value, monkeypatch):
    body = request(value)
    service, port = core(body, bundle)
    monkeypatch.setattr(commands, "execute", lambda *a: pytest.fail("ambient handler was reached"))
    assert service.commit(body, port)["status"] == "refused"
    assert not port.accepts


def test_core_merged_compiler_failure_is_dependent_conflict(bundle, monkeypatch):
    head = edited(bundle, call(select="b-type", text="head title"))
    body = request(call(x=240), dry_run=True)
    service, port = core(body, bundle, head)
    from astrid.sdk.timeline.commands import CommandError
    apply = service._apply
    resolve = Checkout.resolve
    rebasing = False
    def prepared(*args):
        nonlocal rebasing
        value = apply(*args)
        rebasing = True
        return value
    def fail(self):
        if rebasing:
            raise CommandError("rebased timing replaced explicit write")
        return resolve(self)
    monkeypatch.setattr(service, "_apply", prepared)
    monkeypatch.setattr(Checkout, "resolve", fail)
    result = service.commit(body, port)
    assert result["status"] == "conflict" and result["conflicts"][0]["kind"] == "dependent"
    assert not port.accepts


def test_non_stale_acceptance_error_is_not_retried(bundle):
    body = request(call(x=240))
    service, port = core(body, bundle)
    class RequestIdConflict(Exception):
        code = "conflict"
        details = {"code": "RequestIdConflict", "stored_request_hash": "original"}
    def race(*a):
        raise RequestIdConflict()
    port.on_accept = race
    with pytest.raises(RequestIdConflict):
        service.commit(body, port)
    assert len(port.accepts) == 1


def test_timeline_locks_are_shared_between_factories_and_do_not_block_other_timelines():
    entered, release, second_started, second_read = Event(), Event(), Event(), Event()
    first = request()
    same = request()
    other = request(timeline="other")
    p1, p2, p3 = Port(first), Port(same), Port(other)
    first_revision = p1.revision
    def block(rid):
        entered.set()
        assert release.wait(5)
        return first_revision(rid)
    p1.revision = block
    second_revision = p2.revision
    def observe(rid):
        second_read.set()
        return second_revision(rid)
    p2.revision = observe
    def second():
        second_started.set()
        return create_runtime_commit_service().commit(same, p2)
    with ThreadPoolExecutor(max_workers=3) as pool:
        a = pool.submit(create_runtime_commit_service().commit, first, p1)
        try:
            assert entered.wait(5)
            b = pool.submit(second)
            assert second_started.wait(5)
            c = pool.submit(create_runtime_commit_service().commit, other, p3)
            assert c.result(5)["status"] == "no_change"
            assert not second_read.is_set()
        finally:
            release.set()
        assert a.result(5)["status"] == b.result(5)["status"] == "no_change"


def exact_closure(bundle, revision_id="rev-0"):
    """Shape of C's exact detached port result, built from real authoring output."""
    published = compile_authoring_candidate(bundle).publication
    parent = dict(revision_id=revision_id, project_id="p", timeline_id="t",
                  payload=published["parent_composition"], content_digest=_digest(published["parent_composition"]),
                  created_at="fixture")
    shots = [{**row, "project_id": "p"} for row in published["shot_revisions"]]
    internals = [{**row, "project_id": "p"} for row in published["internal_timeline_revisions"]]
    return dict(parent_revision=parent, shot_revisions=shots, internal_timeline_revisions=internals)


@pytest.mark.parametrize("scope", ["parent", "occurrence:occ-A"])
def test_exact_request_bound_closure_opens_pinned_children_without_revision_fallback(bundle, scope):
    body = request(call(x=240))
    body['scope']['target'] = scope
    port = Port(body)
    closure = exact_closure(bundle)
    original = copy.deepcopy(closure)
    reads = []
    def closed(rid):
        reads.append(rid)
        assert rid == "rev-0"
        return copy.deepcopy(closure)
    port.revision_closure = closed
    port.revision = lambda *a: pytest.fail("unbound parent/child read")
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "accepted" and len(port.accepts) == 1 and reads == ["rev-0"]
    assert Checkout(result["canonical_document"]).clip("a-rocket").get("x") == 240
    assert closure == original


@pytest.mark.parametrize("damage", ["shot-missing", "internal-missing", "extra-shot", "shot-project",
                                    "internal-project", "shot-pin", "child-digest", "parent-timeline"])
def test_exact_closure_corruption_never_accepts_or_substitutes_heads(bundle, damage):
    body = request(call(x=240), dry_run=True)
    port = Port(body)
    closure = exact_closure(bundle)
    if damage == "shot-missing":
        closure["shot_revisions"].pop()
    elif damage == "internal-missing":
        closure["internal_timeline_revisions"].pop()
    elif damage == "extra-shot":
        closure["shot_revisions"].append(copy.deepcopy(closure["shot_revisions"][0]))
    elif damage == "shot-project":
        closure["shot_revisions"][0]["project_id"] = "other"
    elif damage == "internal-project":
        closure["internal_timeline_revisions"][0]["project_id"] = "other"
    elif damage == "shot-pin":
        closure["shot_revisions"][0]["internal_timeline_revision_id"] = "unbound"
    elif damage == "child-digest":
        closure["internal_timeline_revisions"][0]["content_digest"] = "sha256:" + "0" * 64
    else:
        closure["parent_revision"]["timeline_id"] = "other"
    port.revision_closure = lambda rid: copy.deepcopy(closure)
    port.head = lambda: pytest.fail("read new head after invalid exact base")
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "refused" and not port.accepts
    assert all(result[key] is None for key in ("previous_revision_id", "accepted_revision_id", "runtime_receipt",
                                               "provenance", "evidence"))


def test_unsupported_occurrence_scope_keeps_explicit_refusal():
    body = request()
    body["scope"]["target"] = "occurrence:missing"
    port = Port(body)
    result = create_runtime_commit_service().commit(body, port)
    assert result["status"] == "refused" and not port.accepts


def test_stalehead_replays_canonical_nudge_and_not_the_relative_handler(bundle, monkeypatch):
    from dataclasses import replace
    entry = commands.REGISTRY['clip.nudge']
    invocations = []
    def nudge(owner, seconds=0., *, frames=0):
        invocations.append((seconds, frames))
        return entry.handler(owner, seconds, frames=frames)
    monkeypatch.setitem(commands.REGISTRY, 'clip.nudge', replace(entry, handler=nudge))
    body = request(call('clip.nudge', frames=9))
    service, port = core(body, bundle)
    def race(p, prepared):
        if len(p.accepts) <= 2:
            p.current = 'race-' + str(len(p.accepts))
            p.bundles[p.current] = copy.deepcopy(bundle)
            raise StaleHead()
    port.on_accept = race
    result = service.commit(body, port)
    assert result['status'] == 'accepted' and len(port.accepts) == 3
    assert invocations == [(0., 9)]
    assert all(p['result']['change']['commands'][0]['op'] == 'clip.on' for p in port.accepts)
    assert len({json.dumps(p['evidence']['commands'], sort_keys=True) for p in port.accepts}) == 1


def test_exact_occurrence_scope_uses_current_identity_model_and_refuses_escape(bundle):
    body = request(call(select='occurrence:occ-A/clip:a-rocket', x=240))
    body['scope'].update(target='occurrence:occ-A', content='this-occurrence', shot_id='A',
                         shot_revision_id='rev-shot-A', internal_revision_id='rev-it-A')
    service, port = core(body, bundle)
    result = service.commit(body, port)
    assert result['status'] == 'accepted'
    assert result['change']['commands'][0]['resolved'] == ['occurrence:occ-A/clip:a-rocket']
    for damage in ('other-target', 'wrong-pin', 'shared-shot'):
        changed = copy.deepcopy(body)
        if damage == 'other-target':
            changed['change']['commands'] = [call(select='b-type', text='outside')]
        elif damage == 'wrong-pin':
            changed['scope']['shot_revision_id'] = 'not-the-pinned-revision'
        else:
            changed['scope']['content'] = 'shared-shot'
        service, port = core(changed, bundle)
        assert service.commit(changed, port)['status'] == 'refused'
        assert not port.accepts


def test_exact_shared_child_with_duplicate_timing_identities_is_explicit_refusal(bundle):
    closure = exact_closure(bundle)
    parent = closure['parent_revision']['payload']
    occurrence = copy.deepcopy(parent['occurrences'][0])
    occurrence.update(occurrence_id='occ-copy', placement={'start_ms': 8000})
    parent['occurrences'].append(occurrence)
    closure['parent_revision']['content_digest'] = _digest(parent)
    body = request(call(select='occurrence:occ-copy/clip:a-rocket', x=240))
    body['scope'].update(target='occurrence:occ-copy', content='this-occurrence')
    port = Port(body)
    port.revision_closure = lambda rid: copy.deepcopy(closure)
    result = create_runtime_commit_service().commit(body, port)
    assert result['status'] == 'refused' and not port.accepts
    assert 'Ambiguous occurrence timing identities' in result['error']['message']
    assert 'cut:c1' in result['error']['message'] and 'line:s1' in result['error']['message']
    assert result['id_map'] == {}  # no unsupported fork/identity result is invented
