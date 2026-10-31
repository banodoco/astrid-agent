"""Deterministic multi-actor clash-back journey against a real Runtime."""

from __future__ import annotations

import copy
import hashlib
import json
import threading

import pytest

from tests.sdk.test_timeline_editing_acceptance_runtime import _media_file, _timeline_history

pytest_plugins = ("tests.sdk.test_timeline_editing_acceptance_runtime",)


def _actor(daemon, project, tmp_path, name, *, outbox=None):
    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision(
        name,
        ["handshake", "projects:read", "projects:write", "objects:read", "objects:write",
         "tasks:read", "tasks:write"],
    )
    client = open_client(daemon, actor=name, credential=credential)
    timeline = (Timeline.open(project, "film", client=client) if outbox is None else
                Timeline.open(project, "film", client=client, outbox_path=outbox))
    return client, timeline


def _injected_conflict(timeline, *, ref, path):
    from astrid_runtime_client import ApiError

    return ApiError(409, "conflict", "injected definitive conflict", details={
        "status": "conflict",
        "reason": "value_conflict",
        "conflicts": [{"ref": ref, "path": list(path)}],
        "current": dict(timeline.head),
    })


def _raise_injected_conflict(timeline, **kwargs):
    raise _injected_conflict(timeline, **kwargs)


def _set_clip(timeline, clip_id, **values):
    parent = timeline.bundle["parent"]
    for root in (parent, parent.get("config", {})):
        clip = next((row for row in root.get("clips", []) if row["id"] == clip_id), None)
        if clip is not None:
            clip.update(values)


def _clips(document):
    return {
        row["id"]: {key: row[key] for key in ("at", "volume", "opacity") if key in row}
        for row in document["parent"]["clips"]
    }


def test_new_file_swap_recovery_reimports_after_saved_runtime_refusal(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitRefused

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "swap-recovery-data"))
    source = _media_file(tmp_path / "swap-seed.png")
    timeline.commit(
        lambda t: t.add(source, at=0, hold=3, track="V1", id="swap-target"),
        label="Seed swap target", key="swap-recovery-seed",
    )
    stale = Timeline.open(
        project, "film", client=client, outbox_path=tmp_path / "swap-own-outbox.json"
    )
    replacement = _media_file(tmp_path / "swap-replacement.png", color="blue")
    replacement_digest = "sha256:" + hashlib.sha256(replacement.read_bytes()).hexdigest()
    evaluations = []

    def edit(t):
        evaluations.append(1)
        t.clip("swap-target").swap_asset(str(replacement))

    generated = client._remote._transport._generated
    original = generated.commit_timeline
    first_attempt = []

    def refuse_first_attempt(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "Recover new file swap" and not first_attempt:
            first_attempt.append(copy.deepcopy(body))
            from astrid_runtime_client import ApiError

            raise ApiError(409, "invalid_records", "simulated saved swap refusal", details={
                "status": "refused", "reason": "invalid_records", "current": dict(stale.head),
            })
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", refuse_first_attempt)
    with pytest.raises(CommitRefused) as caught:
        stale.commit(edit, label="Recover new file swap", key="swap-new-file-recovery")

    pending_id = caught.value.pending_id
    assert pending_id
    saved = stale.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["failure"] == caught.value.failure
    assert saved["failure"]["details"]["status"] == "refused"
    assert saved["failure"]["details"]["reason"] == "invalid_records"
    assert saved["parts"][0]["imports"][0]["object_id"] == replacement_digest
    assert len(evaluations) == 1

    monkeypatch.setattr(generated, "commit_timeline", original)
    reopened = Timeline.open(
        project, "film", client=client, outbox_path=tmp_path / "swap-own-outbox.json"
    )
    landed = reopened.retry(pending_id, fn=edit)

    assert landed["status"] in {"accepted", "merged", "landed"}
    assert len(evaluations) == 2
    assert len(landed["imports"]) == 1
    assert landed["imports"][0]["object_id"] == replacement_digest
    assert replacement_digest in str(landed["data"]["canonical_document"])
    assert reopened.pending_show(pending_id)["state"] == "landed"


def test_new_file_swap_recovers_after_same_actor_default_outbox_race(
    editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, _setup = editing_case
    from astrid.sdk.timeline import Timeline

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "swap-recovery-data"))
    timeline = Timeline.open(project, "film", client=client)
    source = _media_file(tmp_path / "swap-seed.png")
    timeline.commit(
        lambda t: t.add(source, at=0, hold=3, track="V1", id="swap-target"),
        label="Seed swap target", key="swap-recovery-seed",
    )
    stale = Timeline.open(project, "film", client=client)
    peer = Timeline.open(project, "film", client=client)
    assert stale.session.outbox.path == peer.session.outbox.path
    assert stale.head == peer.head
    replacement = _media_file(tmp_path / "swap-replacement.png", color="blue")
    replacement_digest = "sha256:" + hashlib.sha256(replacement.read_bytes()).hexdigest()
    evaluations = []
    imported = threading.Event()
    resume = threading.Event()
    outcomes = []

    def edit(t):
        evaluations.append(1)
        t.clip("swap-target").swap_asset(str(replacement))

    from astrid.sdk.timeline import media_add

    select = media_add._selection

    def import_then_pause(*args, **kwargs):
        selected = select(*args, **kwargs)
        imported.set()
        if not resume.wait(timeout=60):
            raise TimeoutError("same-actor peer did not release the imported swap")
        return selected

    monkeypatch.setattr(media_add, "_selection", import_then_pause)

    def run_swap():
        try:
            outcomes.append(stale.commit(
                edit, label="Recover new file swap", key="swap-new-file-recovery"
            ))
        except Exception as exc:  # Return the actual Runtime/outbox failure to the test thread.
            outcomes.append(exc)

    worker = threading.Thread(target=run_swap)
    worker.start()
    try:
        assert imported.wait(timeout=60), "function did not import its local replacement"
        peer_result = peer.commit(
            lambda t: t.bundle["parent"].update({"name": "Peer advanced shared outbox"}),
            label="Disjoint same-actor peer edit", key="swap-shared-outbox-peer",
        )
        assert peer_result["status"] in {"accepted", "merged", "landed"}
    finally:
        resume.set()
        worker.join(timeout=60)
    assert not worker.is_alive(), "swap function did not finish after peer commit"
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict), outcomes
    landed = outcomes[0]
    assert landed["status"] in {"accepted", "merged", "landed"}
    assert landed["receipt"]["receipt_id"]
    reopened = Timeline.open(project, "film", client=client)
    assert reopened.session.outbox.path == peer.session.outbox.path
    assert len(evaluations) == 2
    assert reopened.pending_list() == []
    assert reopened.bundle["parent"]["name"] == "Peer advanced shared outbox"
    assert replacement_digest in str(landed["data"]["canonical_document"])
    history = client.timelines.history(project, "film", limit=100).data["items"]
    labels = [record.get("label") for record in history]
    assert labels.count("Recover new file swap") == 1
    assert labels.count("Disjoint same-actor peer edit") == 1


def test_reviewed_new_file_swap_runtime_conflict_is_saved_and_explicit_retry_recovers(
    editing_case, tmp_path, monkeypatch,
):
    daemon, _client, project, setup = editing_case
    from astrid.sdk.timeline.sync import CommitConflict

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "reviewed-swap-data"))
    source = _media_file(tmp_path / "reviewed-swap-seed.png")
    setup.commit(
        lambda t: t.add(source, at=0, hold=3, track="V1", id="reviewed-swap-target"),
        label="Seed reviewed swap target", key="reviewed-swap-seed",
    )
    _, stale = _actor(
        daemon, project, tmp_path, "reviewed-swap-author",
        outbox=tmp_path / "reviewed-swap-author.json",
    )
    _, peer = _actor(
        daemon, project, tmp_path, "reviewed-swap-peer",
        outbox=tmp_path / "reviewed-swap-peer.json",
    )
    replacement = _media_file(tmp_path / "reviewed-swap-replacement.png", color="blue")
    peer_replacement = _media_file(tmp_path / "reviewed-swap-peer.png", color="green")
    replacement_digest = "sha256:" + hashlib.sha256(replacement.read_bytes()).hexdigest()
    evaluations = []

    def edit(t):
        evaluations.append(True)
        t.clip("reviewed-swap-target").swap_asset(str(replacement))

    reviewed = stale.commit(edit, label="Reviewed new file swap", dry_run=True)
    assert reviewed["status"] == "preview"
    commands = copy.deepcopy(reviewed["data"]["commands"])
    assert commands
    assert len(evaluations) == 1

    peer_result = peer.commit(
        lambda t: t.clip("reviewed-swap-target").swap_asset(str(peer_replacement)),
        label="Peer replaces reviewed swap target", key="reviewed-swap-peer",
    )
    assert peer_result["status"] in {"accepted", "merged", "landed"}
    peer_digest = "sha256:" + hashlib.sha256(peer_replacement.read_bytes()).hexdigest()
    assert peer_digest in str(peer_result["data"]["canonical_document"])

    with pytest.raises(CommitConflict) as caught:
        stale.commit(
            commands, label="Reviewed new file swap", key="reviewed-new-file-swap",
        )

    pending_id = caught.value.pending_id
    assert pending_id
    assert caught.value.failure["details"]["status"] == "conflict"
    saved = stale.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "list"
    assert saved["failure"] == caught.value.failure
    assert saved["parts"][0]["commands"] == commands
    assert saved["parts"][0]["imports"][0]["object_id"] == replacement_digest
    assert len(evaluations) == 1

    landed = stale.retry(pending_id, fn=edit, overwrite=True)

    assert landed["status"] in {"accepted", "merged", "landed", "rebased"}
    assert len(evaluations) == 2
    assert len(landed["imports"]) == 1
    assert landed["imports"][0]["object_id"] == replacement_digest
    assert replacement_digest in str(landed["data"]["canonical_document"])
    final = stale.pending_show(pending_id)
    assert final["state"] == "landed"
    assert final["failure"] == caught.value.failure


def test_same_value_reviewed_list_recovers_through_shared_outbox_cli(
    editing_case, tmp_path, monkeypatch, capsys,
):
    daemon, client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "shared-outbox-data"))

    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from astrid.core.cli.domain_product import run_product_family

    _, credential = daemon.credentials.provision(
        "same-value-list-peer",
        ["handshake", "projects:read", "projects:write", "objects:read", "objects:write",
         "tasks:read", "tasks:write"],
    )
    peer_client = open_client(daemon, actor="same-value-list-peer", credential=credential)
    stale = Timeline.open(project, "film", client=client)
    peer = Timeline.open(project, "film", client=peer_client,
                         outbox_path=tmp_path / "same-value-peer.json")
    desired = "same value from both actors"
    commands = stale.commit(
        lambda t: t.bundle["parent"].update({"name": desired}),
        label="Same value reviewed list", dry_run=True, merge="fields", reads=[],
    )["data"]["commands"]

    peer_result = peer.commit(
        lambda t: t.bundle["parent"].update({"name": desired}),
        label="Peer same value", key="same-value-peer-write",
    )
    assert peer_result["receipt"]["receipt_id"]
    history_before_retry = len(client.timelines.history(project, "film", limit=100).data["items"])

    from astrid.sdk.timeline.sync import CommitConflict

    with pytest.raises(CommitConflict) as caught:
        stale.commit(commands, label="Same value reviewed list", key="same-value-reviewed-list",
                     merge="fields", reads=[])
    pending_id = caught.value.pending_id
    assert pending_id
    assert stale.pending_show(pending_id)["parts"][0]["commands"] == commands

    # The CLI opens the actor's default shared outbox and should detect that the
    # peer already landed the exact requested value without publishing again.
    assert run_product_family(
        "timelines", ["retry", "film", pending_id, "--project", project, "--json"],
        client=client,
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["data"]["status"] == "landed"
    assert output["data"]["pending_id"] == pending_id
    assert "already has your value" in output["data"]["summary"]
    assert len(client.timelines.history(project, "film", limit=100).data["items"]) == history_before_retry
    assert Timeline.open(project, "film", client=client).pending_show(pending_id)["state"] == "landed"


def test_function_commit_uses_head_advanced_by_same_actor_default_outbox(
    editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "shared-function-data"))

    from astrid.sdk.timeline import Timeline

    stale = Timeline.open(project, "film", client=client)
    peer = Timeline.open(project, "film", client=client)
    assert stale.session.outbox.path == peer.session.outbox.path
    assert stale.head == peer.head

    peer_result = peer.commit(
        lambda t: t.bundle["parent"].update({"name": "same-actor peer edit"}),
        label="Same actor peer edit", key="shared-function-peer",
    )
    assert peer_result["status"] in {"accepted", "merged", "landed"}

    observed_names = []

    def edit_on_head(t):
        observed_names.append(t.bundle["parent"]["name"])
        t.bundle["parent"]["custom"] = {"function_saw_peer_head": True}

    landed = stale.commit(
        edit_on_head, label="Function after shared outbox advance",
        key="shared-function-after-peer", reads=[],
    )

    assert landed["status"] in {"accepted", "merged", "landed"}
    assert observed_names == ["same-actor peer edit"]
    assert stale.bundle["parent"]["name"] == "same-actor peer edit"
    assert stale.bundle["parent"]["custom"] == {"function_saw_peer_head": True}
    assert stale.pending_list() == []


def test_function_commit_keeps_overlap_as_pending_clash_after_shared_outbox_advance(
    editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "shared-overlap-data"))

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict

    stale = Timeline.open(project, "film", client=client)
    peer = Timeline.open(project, "film", client=client)
    peer.commit(
        lambda t: t.bundle["parent"].update({"name": "peer owns this field"}),
        label="Same actor overlapping peer edit", key="shared-overlap-peer",
    )

    evaluations = []

    def overlapping_edit(t):
        evaluations.append(t.bundle["parent"]["name"])
        t.bundle["parent"]["name"] = "function overwrites peer"

    with pytest.raises(CommitConflict) as caught:
        stale.commit(
            overlapping_edit, label="Overlapping function after shared outbox advance",
            key="shared-overlap-function", reads=[],
        )

    assert evaluations == ["peer owns this field"]
    assert caught.value.failure["details"]["reason"] == "intervening_write"
    assert caught.value.pending_id
    saved = stale.pending_show(caught.value.pending_id)
    assert saved["state"] == "pending"
    assert peer.bundle["parent"]["name"] == "peer owns this field"

    pending_id = caught.value.pending_id
    reopened = Timeline.open(project, "film", client=client)
    with pytest.raises(ValueError, match="overwrite requires the original edit function"):
        reopened.retry(pending_id, overwrite=True)
    assert reopened.pending_show(pending_id)["state"] == "pending"

    landed = reopened.retry(pending_id, fn=overlapping_edit, overwrite=True)

    assert landed["status"] == "rebased"
    assert len(evaluations) == 2
    assert landed["receipt"]["receipt_id"]
    assert reopened.bundle["parent"]["name"] == "function overwrites peer"
    saved = Timeline.open(project, "film", client=client).pending_show(pending_id)
    assert saved["state"] == "landed"
    assert saved["closed_by"]["publication_id"] == landed["receipt"]["receipt_id"]


def test_function_rerun_overlap_during_same_actor_shared_outbox_advance_is_saved(
    editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "shared-rerun-overlap-data"))

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict

    stale = Timeline.open(project, "film", client=client)
    peer = Timeline.open(project, "film", client=client)
    assert stale.session.outbox.path == peer.session.outbox.path
    assert stale.head == peer.head
    initial_head = dict(stale.head)
    initial_name = stale.bundle["parent"]["name"]

    evaluating = threading.Event()
    resume = threading.Event()
    observed_names = []
    outcomes = []

    def overlapping_edit(t):
        observed_names.append(t.bundle["parent"]["name"])
        if len(observed_names) == 1:
            evaluating.set()
            if not resume.wait(timeout=60):
                raise TimeoutError("same-actor peer did not release the rerunning function")
        t.bundle["parent"]["name"] = "function overwrites peer"

    def run_function():
        try:
            outcomes.append(stale.commit(
                overlapping_edit, label="Overlapping function during shared outbox advance",
                key="shared-rerun-overlap-function", reads=[],
            ))
        except Exception as exc:  # Return the actual Runtime/outbox failure to the test thread.
            outcomes.append(exc)

    worker = threading.Thread(target=run_function)
    worker.start()
    try:
        assert evaluating.wait(timeout=60), "function did not pause during its first evaluation"
        peer_result = peer.commit(
            lambda t: t.bundle["parent"].update({"name": "peer owns this field"}),
            label="Same actor peer edit during function evaluation", key="shared-rerun-peer",
        )
        assert peer_result["status"] in {"accepted", "merged", "landed"}
    finally:
        resume.set()
        worker.join(timeout=60)

    assert not worker.is_alive(), "function did not finish after peer commit"
    assert len(outcomes) == 1 and isinstance(outcomes[0], CommitConflict), outcomes
    conflict = outcomes[0]
    assert observed_names == [initial_name, "peer owns this field"]
    assert conflict.failure["details"]["reason"] == "intervening_write"
    assert conflict.pending_id

    saved = stale.pending_show(conflict.pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["parts"][0]["base"] == initial_head
    assert saved["clash"]["schema"] == "astrid.clash/2"
    assert saved["clash"]["conflicts"] == conflict.failure["details"]["conflicts"]
    assert len(saved["clash"]["receipts"]) == 1
    intervening = saved["clash"]["receipts"][0]
    assert intervening["publication_id"] == peer_result["receipt"]["receipt_id"]
    assert intervening["label"] == "Same actor peer edit during function evaluation"
    assert intervening["touches_conflict"] is True
    assert saved["parts"][0]["commands"]
    assert "function overwrites peer" in json.dumps(saved["parts"][0]["commands"])
    assert peer.bundle["parent"]["name"] == "peer owns this field"


def test_pending_rederive_lost_reply_replays_exact_commit_once(editing_case, tmp_path, monkeypatch):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict, CommitOutcomeUnknown, TimelineEditError
    from astrid_runtime_client import ApiError
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision(
        "j6-f4-peer",
        ["handshake", "projects:read", "projects:write", "objects:read", "objects:write",
         "tasks:read", "tasks:write"],
    )
    peer_client = open_client(daemon, actor="j6-f4-peer", credential=credential)
    peer = Timeline.open(project, "film", client=peer_client, outbox_path=tmp_path / "f4-peer.json")

    stale_commands = timeline.commit(
        lambda t: t.bundle["parent"].update({"name": "stale edit"}),
        label="F4 pending edit", dry_run=True,
    )["data"]["commands"]
    peer.commit(
        lambda t: t.bundle["parent"].update({"name": "peer edit"}),
        label="F4 peer edit", key="j6-f4-peer-edit",
    )
    with pytest.raises(CommitConflict) as caught:
        timeline.commit(stale_commands, label="F4 pending edit", key="j6-f4-edit")
    pending_id = caught.value.pending_id
    assert pending_id
    assert timeline.retry(pending_id)["status"] == "re_derive"

    timeline = Timeline.open(
        project, "film", client=client, outbox_path=timeline.session.outbox.path
    )
    commands = timeline.commit(
        lambda t: t.bundle["parent"].update({"name": "rederived edit"}),
        label="F4 pending edit", dry_run=True,
    )["data"]["commands"]
    assert commands != stale_commands

    generated = client._remote._transport._generated
    original = generated.commit_timeline
    sent = []

    def lose_first_reply(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "F4 pending edit":
            sent.append((copy.deepcopy(body), kwargs["idempotency_key"]))
            result = original(project_id, timeline_id, body, **kwargs)
            if len(sent) == 1:
                raise ApiError(503, "not_ready", "simulated lost F4 reply after acceptance")
            return result
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", lose_first_reply)
    history_before = _timeline_history(client, project, "film")
    with pytest.raises(CommitOutcomeUnknown, match="No reply could be verified"):
        timeline.commit(commands, label="F4 pending edit", key="j6-f4-edit", pending=pending_id)

    recovered = timeline.commit(
        commands, label="F4 pending edit", key="j6-f4-edit", pending=pending_id
    )
    duplicate = timeline.commit(
        commands, label="F4 pending edit", key="j6-f4-edit", pending=pending_id
    )
    assert recovered["receipt"] == duplicate["receipt"]
    assert len(sent) == 3
    assert sent[0] == sent[1] == sent[2]
    assert len(_timeline_history(client, project, "film")) == len(history_before) + 1

    changed_commands = copy.deepcopy(commands)
    changed_commands[0]["args"]["changes"][0]["value"] = "changed under the same key"
    with pytest.raises(TimelineEditError, match="idempotency key belongs to another edit"):
        timeline.commit(
            changed_commands, label="F4 pending edit", key="j6-f4-edit", pending=pending_id
        )
    assert len(sent) == 3
    assert len(_timeline_history(client, project, "film")) == len(history_before) + 1


def _a5_clash_process(endpoint, credential, realm, project, result_queue):
    from astrid.sdk.client import AstridClient
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict

    client = AstridClient.open(
        endpoint=endpoint,
        credential=credential,
        realm_id=realm,
        actor_id="j6-a5",
        client_name="j6-clash-recovery",
        client_version="1",
        protocol_version="workspace.v1",
    )
    timeline = Timeline.open(project, "film", client=client)
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    injected = []
    evaluations = []

    def exhaust_a5(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "A5 opacity" and len(injected) < 3:
            injected.append(body)
            raise _injected_conflict(timeline, ref="parent/clip:c6", path=("opacity",))
        return original(project_id, timeline_id, body, **kwargs)

    generated.commit_timeline = exhaust_a5

    def a5_edit(t):
        evaluations.append(1)
        _set_clip(t, "c6", opacity=0.5)

    try:
        timeline.commit(a5_edit, label="A5 opacity", key="j6-a5")
    except CommitConflict as caught:
        result_queue.put({
            "pending_id": caught.pending_id,
            "injected": len(injected),
            "evaluations": len(evaluations),
        })
    else:
        result_queue.put({"error": "A5 commit unexpectedly succeeded"})


def _same_actor_function_process(endpoint, credential, realm, project, captured_queue,
                                  release, result_queue):
    """Pause after function commands are captured while a same-actor peer commits."""
    from astrid.sdk.client import AstridClient
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict

    client = AstridClient.open(
        endpoint=endpoint,
        credential=credential,
        realm_id=realm,
        actor_id="ux-core-same-actor",
        client_name="ux-core-clash-recovery",
        client_version="1",
        protocol_version="workspace.v1",
    )
    timeline = Timeline.open(project, "film", client=client)
    capture = Timeline._capture_free_edits

    def capture_then_wait(working):
        capture(working)
        if working._commands:
            captured_queue.put({"base": copy.deepcopy(working._basis),
                                "commands": copy.deepcopy(working._commands),
                                "outbox": str(working.session.outbox.path)})
            if not release.wait(timeout=60):
                raise TimeoutError("peer did not release captured function edit")

    Timeline._capture_free_edits = capture_then_wait
    evaluations = []

    def edit(working):
        evaluations.append(1)
        working.bundle["parent"]["name"] = "Same actor function edit"

    try:
        timeline.commit(edit, label="Same actor function edit", key="ux-core-function",
                        reads=[])
    except CommitConflict as caught:
        result_queue.put({"pending_id": caught.pending_id, "evaluations": len(evaluations)})
    except Exception as caught:  # Return child failures to the parent as test evidence.
        import traceback
        result_queue.put({"error": repr(caught), "traceback": traceback.format_exc()})
    else:
        result_queue.put({"error": "same-actor commit unexpectedly succeeded",
                          "evaluations": len(evaluations)})


def _abrupt_function_process(endpoint, credential, realm, project, captured_queue,
                             release, result_queue):
    """Exit without normal teardown once the durable refusal has been saved."""
    import os

    from astrid.sdk.client import AstridClient
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict

    client = AstridClient.open(
        endpoint=endpoint,
        credential=credential,
        realm_id=realm,
        actor_id="ux-core-abrupt",
        client_name="ux-core-abrupt-recovery",
        client_version="1",
        protocol_version="workspace.v1",
    )
    timeline = Timeline.open(project, "film", client=client)
    capture = Timeline._capture_free_edits

    def capture_then_wait(working):
        capture(working)
        if working._commands:
            captured_queue.put({"base": copy.deepcopy(working._basis),
                                "commands": copy.deepcopy(working._commands),
                                "outbox": str(working.session.outbox.path)})
            if not release.wait(timeout=60):
                raise TimeoutError("peer did not release captured function edit")

    Timeline._capture_free_edits = capture_then_wait

    def edit(working):
        working.bundle["parent"]["name"] = "Abrupt function edit"

    try:
        timeline.commit(edit, label="Abrupt refusal recovery", key="ux-core-abrupt-edit", reads=[])
    except CommitConflict as caught:
        result_queue.put({"pending_id": caught.pending_id})
        result_queue.close()
        result_queue.join_thread()
        os._exit(31)
    except BaseException as caught:
        result_queue.put({"error": repr(caught)})
        result_queue.close()
        result_queue.join_thread()
        os._exit(32)
    result_queue.put({"error": "abrupt child commit unexpectedly succeeded"})
    result_queue.close()
    result_queue.join_thread()
    os._exit(33)


def _same_actor_reviewed_list_process(endpoint, credential, realm, project, captured_queue,
                                      release, result_queue):
    """Pause after a reviewed command list is captured while a same-actor peer commits."""
    from astrid.sdk.client import AstridClient
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict

    client = AstridClient.open(
        endpoint=endpoint,
        credential=credential,
        realm_id=realm,
        actor_id="ux-core-same-actor",
        client_name="ux-core-reviewed-recovery",
        client_version="1",
        protocol_version="workspace.v1",
    )
    timeline = Timeline.open(project, "film", client=client)
    preview = timeline.commit(
        lambda working: working.bundle["parent"].update(
            {"name": "Same actor reviewed list edit"}),
        label="Same actor reviewed list",
        dry_run=True,
    )
    commands = copy.deepcopy(preview["data"]["commands"])
    captured_queue.put({"base": copy.deepcopy(timeline._basis),
                        "commands": commands,
                        "outbox": str(timeline.session.outbox.path)})
    try:
        if not release.wait(timeout=60):
            raise TimeoutError("peer did not release captured reviewed list")
        timeline.commit(commands, label="Same actor reviewed list", key="ux-core-reviewed")
    except CommitConflict as caught:
        result_queue.put({"pending_id": caught.pending_id})
    except Exception as caught:  # Return child failures to the parent as test evidence.
        import traceback
        result_queue.put({"error": repr(caught), "traceback": traceback.format_exc()})
    else:
        result_queue.put({"error": "same-actor reviewed-list commit unexpectedly succeeded"})


def _reciprocal_retry_process(endpoint, credential, realm, project, actor, pending_id,
                              barrier, result_queue):
    """Retry a peer's saved conflict and refuse both requests at the same barrier."""
    from astrid.sdk.client import AstridClient
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitRefused

    client = AstridClient.open(
        endpoint=endpoint,
        credential=credential,
        realm_id=realm,
        actor_id=actor,
        client_name="ux-core-reciprocal-retry",
        client_version="1",
        protocol_version="workspace.v1",
    )
    timeline = Timeline.open(project, "film", client=client)
    generated = client._remote._transport._generated

    def refuse_together(*_args, **_kwargs):
        barrier.wait(timeout=30)
        from astrid_runtime_client import ApiError

        raise ApiError(422, "invalid_records", "injected retry refusal", details={
            "status": "refused", "reason": "invalid_records", "current": dict(timeline.head),
        })

    generated.commit_timeline = refuse_together
    try:
        preview = timeline.retry(pending_id)
        result = timeline.commit(preview)
    except CommitRefused as caught:
        saved = timeline.pending_show(caught.pending_id)
        assert saved["failure"] == caught.failure
        result_queue.put({"pending_id": pending_id, "status": "refused",
                          "saved_pending_id": caught.pending_id,
                          "failure": caught.failure})
    except Exception as caught:  # Return child failures to the parent as test evidence.
        import traceback
        result_queue.put({"pending_id": pending_id, "error": repr(caught),
                          "traceback": traceback.format_exc()})
    else:
        result_queue.put({"pending_id": pending_id, "status": result.get("status")})


def test_five_agents_deterministic(editing_case, tmp_path, monkeypatch):
    daemon, _client, project, setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "astrid-data"))

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.sync import CommitConflict

    source = _media_file(tmp_path / "five-agents.png")

    def seed(t):
        for index in range(1, 7):
            t.add(source, at=(index - 1) * 5, hold=2, track="V1", id=f"c{index}")

    setup.commit(seed, label="Seed six clips", key="j6-seed")
    initial_head = dict(setup.head)
    baseline = copy.deepcopy(_clips(setup._base_bundle))

    actors = {
        name: _actor(
            daemon,
            project,
            tmp_path,
            name,
            outbox=None if name == "j6-a1" else tmp_path / f"{name}.json",
        )
        for name in ("j6-a1", "j6-a2", "j6-a3", "j6-a4")
    }
    a1_client, a1 = actors["j6-a1"]
    _, a2 = actors["j6-a2"]
    a3_client, a3 = actors["j6-a3"]
    a4_client, a4 = actors["j6-a4"]
    assert all(timeline.head == initial_head for _, timeline in actors.values())

    def a1_edit(t):
        _set_clip(t, "c1", volume=0.5)

    a3_runs = []

    def a3_edit(t):
        a3_runs.append(1)
        t.move("parent/clip:c2", on=6.0)
        _set_clip(t, "c3", opacity=0.8)

    a4_runs = []

    def a4_edit(t):
        a4_runs.append(1)
        _set_clip(t, "c4", volume=0.3)
        _set_clip(t, "c5", volume=0.3)
        t.move("parent/clip:c2", on=8.0)

    # Capture both reviewed lists from the same head before releasing any writer.
    a2_commands = a2.commit(
        lambda t: t.move("parent/clip:c2", on=4.0), label="A2 move", dry_run=True
    )["data"]["commands"]
    a3_commands = a3.commit(a3_edit, label="A3 edits", dry_run=True)["data"]["commands"]
    a4_commands = a4.commit(a4_edit, label="A4 batch", dry_run=True)["data"]["commands"]
    assert len(a3_runs) == len(a4_runs) == 1

    # Functions retain their default atomic merge mode; no reads override is supplied.
    accepted = a1.commit(a1_edit, label="A1 volume", key="j6-a1")
    assert accepted["status"] == "accepted"

    merged = a2.commit(a2_commands, label="A2 move", key="j6-a2")
    assert merged["status"] == "merged"

    with pytest.raises(CommitConflict) as a3_caught:
        a3.commit(a3_commands, label="A3 edits", key="j6-a3-list")
    a3_id = a3_caught.value.pending_id
    assert a3_id
    assert a3.retry(a3_id)["status"] == "re_derive"
    a3 = Timeline.open(project, "film", client=a3_client, outbox_path=a3.session.outbox.path)
    def a3_rederive(t):
        a3_edit(t)

    a3_preview = a3.commit(a3_rederive, label="A3 edits", dry_run=True)
    assert a3_preview["status"] == "preview"
    a3_landed = a3.commit(
        a3_preview["data"]["commands"], label="A3 edits", key="j6-a3-list", pending=a3_id
    )
    assert a3_landed["status"] in {"accepted", "merged", "landed"}
    assert len(a3_runs) == 2
    assert a3.pending_show(a3_id)["state"] == "landed"

    with pytest.raises(CommitConflict) as a4_caught:
        a4.commit(a4_commands, label="A4 batch", key="j6-a4-list")
    a4_id = a4_caught.value.pending_id
    assert a4_id
    assert a4.retry(a4_id)["status"] == "re_derive"
    a4 = Timeline.open(project, "film", client=a4_client, outbox_path=a4.session.outbox.path)
    def a4_rederive(t):
        a4_edit(t)

    a4_preview = a4.commit(a4_rederive, label="A4 batch", dry_run=True)
    assert a4_preview["status"] == "preview"
    a4_landed = a4.commit(
        a4_preview["data"]["commands"], label="A4 batch", key="j6-a4-list", pending=a4_id
    )
    assert a4_landed["status"] in {"accepted", "merged", "landed"}
    assert len(a4_runs) == 2
    assert a4.pending_show(a4_id)["state"] == "landed"

    import multiprocessing

    _, a5_credential = daemon.credentials.provision(
        "j6-a5",
        ["handshake", "projects:read", "projects:write", "objects:read", "objects:write",
         "tasks:read", "tasks:write"],
    )
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    a5_process = context.Process(
        target=_a5_clash_process,
        args=(daemon.endpoint, a5_credential, daemon.service.realm["id"], project, result_queue),
    )
    a5_process.start()
    a5_process.join(timeout=60)
    if a5_process.is_alive():
        a5_process.terminate()
        a5_process.join(timeout=10)
        pytest.fail("A5 child process did not exit")
    assert a5_process.exitcode == 0
    a5_result = result_queue.get(timeout=5)
    assert "error" not in a5_result, a5_result
    a5_id = a5_result["pending_id"]
    assert a5_id and a5_result["injected"] == 3 and a5_result["evaluations"] == 3
    result_queue.close()

    # A5 has exited. A1 discovers the durable record and publishes its saved-list fallback.
    assert any(record["id"] == a5_id for record in a1.pending_list(actors="all"))
    fallback = a1.retry(a5_id)
    assert fallback["status"] == "preview"
    assert a1.commit(fallback)["status"] in {"accepted", "merged", "landed"}

    all_records = a1.pending_list(actors="all")
    assert not [record for record in all_records if record["state"] == "pending"]
    closed = a1.pending_show(a5_id)
    assert closed["state"] == "landed"
    assert closed["closed_by"]["actor_id"] == "j6-a1"

    expected = copy.deepcopy(baseline)
    expected["c1"]["volume"] = 0.5
    expected["c2"]["at"] = 8.0
    expected["c3"]["opacity"] = 0.8
    expected["c4"]["volume"] = 0.3
    expected["c5"]["volume"] = 0.3
    expected["c6"]["opacity"] = 0.5
    assert _clips(bundle_from_document(a1._runtime_document)) == expected


def test_reciprocal_refused_retries_finish_and_persist(editing_case, tmp_path, monkeypatch):
    daemon, _client, project, setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "reciprocal-retry-data"))

    import json
    import multiprocessing
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    realm = daemon.service.realm["id"]
    actors = {}
    for name in ("ux-retry-a", "ux-retry-b"):
        _, credential = daemon.credentials.provision(name, [
            "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
            "tasks:read", "tasks:write",
        ])
        client = open_client(daemon, actor=name, credential=credential)
        actors[name] = (credential, client, Timeline.open(project, "film", client=client))

    pending_ids = {}
    for name, (_credential, client, timeline) in actors.items():
        preview = timeline.commit(
            lambda t, actor=name: t.bundle["parent"].update({"name": actor}),
            label=f"{name} saved edit", dry_run=True,
        )
        generated = client._remote._transport._generated
        generated.commit_timeline = lambda *_args, _timeline=timeline, **_kwargs: (
            _raise_injected_conflict(_timeline, ref="parent", path=("name",))
        )
        with pytest.raises(CommitConflict) as caught:
            timeline.commit(preview["data"]["commands"], label=f"{name} saved edit")
        pending_ids[name] = caught.value.pending_id
        assert pending_ids[name]
        saved = timeline.pending_show(pending_ids[name])
        assert saved["failure"] == caught.value.failure
        assert saved["failure"]["details"]["status"] == "conflict"

    context = multiprocessing.get_context("spawn")
    barrier, result_queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=_reciprocal_retry_process,
            args=(daemon.endpoint, credential, realm, project, actor,
                  pending_ids[peer], barrier, result_queue),
        )
        for actor, peer, (credential, _client, _timeline) in (
            ("ux-retry-a", "ux-retry-b", actors["ux-retry-a"]),
            ("ux-retry-b", "ux-retry-a", actors["ux-retry-b"]),
        )
    ]
    for process in processes:
        process.start()
    try:
        results = [result_queue.get(timeout=60) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert not process.is_alive(), "reciprocal retry child process did not exit"
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

    assert all(result.get("status") == "refused" for result in results), results
    assert all(result.get("saved_pending_id") == result["pending_id"] for result in results), results
    assert all(result["failure"]["details"]["status"] == "refused" for result in results), results
    assert all(result["failure"]["details"]["reason"] == "invalid_records" for result in results), results
    for actor, (_credential, _client, timeline) in actors.items():
        state = json.loads(timeline.session.outbox.path.read_text(encoding="utf-8"))
        record = state["pending_changes"][pending_ids[actor]]
        assert state["flight"] is None
        assert record["state"] == "pending"
        assert record["failure"]["details"]["status"] == "refused"
    result_queue.close()


def test_same_actor_default_outbox_recovers_captured_function_after_process_exit(
    editing_case, tmp_path, monkeypatch,
):
    daemon, _client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "same-actor-data"))

    import json
    import multiprocessing
    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    actor = "ux-core-same-actor"
    _, credential = daemon.credentials.provision(actor, [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    client = open_client(daemon, actor=actor, credential=credential)
    owner = Timeline.open(project, "film", client=client)
    _, peer = _actor(daemon, project, tmp_path, "ux-core-function-peer",
                     outbox=tmp_path / "ux-core-function-peer.json")
    initial_head = copy.deepcopy(owner.head)

    context = multiprocessing.get_context("spawn")
    captured_queue, result_queue = context.Queue(), context.Queue()
    release = context.Event()
    process = context.Process(
        target=_same_actor_function_process,
        args=(daemon.endpoint, credential, daemon.service.realm["id"], project,
              captured_queue, release, result_queue),
    )
    process.start()
    try:
        captured = captured_queue.get(timeout=60)
        assert captured["base"] == initial_head
        assert captured["commands"]
        assert captured["outbox"] == str(owner.session.outbox.path)

        peer_result = peer.commit(
            lambda t: t.bundle["parent"].update({"name": "Same actor peer edit"}),
            label="Same actor peer edit", key="ux-core-peer",
        )
        assert peer_result["receipt"]["receipt_id"]
        after_peer = json.loads(owner.session.outbox.path.read_text(encoding="utf-8"))
        peer_state = copy.deepcopy(after_peer)
        release.set()
        result = result_queue.get(timeout=60)
        process.join(timeout=10)
        assert not process.is_alive(), "same-actor child process did not exit"
        assert process.exitcode == 0
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    assert "error" not in result, result
    pending_id = result["pending_id"]
    assert pending_id and result["evaluations"] >= 2
    later_peer_result = peer.commit(
        lambda t: t.bundle["parent"].update({"name": "Later same actor peer edit"}),
        label="Later same actor peer edit", key="ux-core-peer-later",
    )
    assert later_peer_result["receipt"]["receipt_id"]
    saved = owner.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["failure"]["details"]["status"] == "conflict"
    assert saved["parts"][0]["base"] == captured["base"]
    assert saved["parts"][0]["commands"]

    after_conflict = json.loads(owner.session.outbox.path.read_text(encoding="utf-8"))
    for key in ("flight", "pending", "acknowledged"):
        assert after_conflict.get(key) == peer_state.get(key)
    assert after_conflict["pending_changes"][pending_id]["parts"][0]["commands"] == saved["parts"][0]["commands"]

    # The callable is gone with the child. Its captured list still conflicts,
    # so retry requests a fresh derivation without publishing or closing it.
    recovered = Timeline.open(project, "film", client=client)
    history_before_retry = len(client.timelines.history(project, "film", limit=100).data["items"])
    packet_before_retry = copy.deepcopy(after_conflict["pending_changes"][pending_id])
    fallback = recovered.retry(pending_id)
    assert fallback["status"] == "re_derive"
    assert len(client.timelines.history(project, "film", limit=100).data["items"]) == history_before_retry
    assert recovered.pending_show(pending_id)["state"] == "pending"
    after_retry = json.loads(owner.session.outbox.path.read_text(encoding="utf-8"))
    assert after_retry["pending_changes"][pending_id] == packet_before_retry
    assert recovered.bundle["parent"]["name"] == "Later same actor peer edit"


def test_abrupt_exit_after_saved_refusal_reopens_and_completes_recovery(
    editing_case, tmp_path, monkeypatch,
):
    daemon, _client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "abrupt-refusal-data"))

    import multiprocessing
    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    actor = "ux-core-abrupt"
    _, credential = daemon.credentials.provision(actor, [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    client = open_client(daemon, actor=actor, credential=credential)
    owner = Timeline.open(project, "film", client=client)
    peer = Timeline.open(project, "film", client=client)
    initial_head = copy.deepcopy(owner.head)

    context = multiprocessing.get_context("spawn")
    captured_queue, result_queue = context.Queue(), context.Queue()
    release = context.Event()
    process = context.Process(
        target=_abrupt_function_process,
        args=(daemon.endpoint, credential, daemon.service.realm["id"], project,
              captured_queue, release, result_queue),
    )
    process.start()
    try:
        captured = captured_queue.get(timeout=60)
        assert captured["base"] == initial_head
        assert captured["commands"]
        assert captured["outbox"] == str(owner.session.outbox.path)

        peer_result = peer.commit(
            lambda t: t.bundle["parent"].update({"name": "Peer wins before crash"}),
            label="Peer wins before crash", key="ux-core-abrupt-peer",
        )
        assert peer_result["receipt"]["receipt_id"]
        release.set()
        process.join(timeout=60)
        assert not process.is_alive(), "abrupt refusal child did not exit"
        assert process.exitcode == 31
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    result = result_queue.get(timeout=5)
    assert "error" not in result, result
    pending_id = result["pending_id"]
    assert pending_id
    reopened = Timeline.open(project, "film", client=client, outbox_path=owner.session.outbox.path)
    saved = reopened.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["failure"]["details"]["status"] == "conflict"

    fallback = reopened.retry(pending_id)
    assert fallback["status"] == "re_derive"
    preview = reopened.commit(
        lambda t: t.bundle["parent"].update({"custom": {"recovered_after_crash": True}}),
        label="Abrupt refusal recovery", dry_run=True,
    )
    landed = reopened.commit(
        preview["data"]["commands"], label="Abrupt refusal recovery",
        key="ux-core-abrupt-edit", pending=pending_id,
    )
    assert landed["status"] in {"accepted", "merged", "landed"}
    assert reopened.bundle["parent"]["name"] == "Peer wins before crash"
    assert reopened.bundle["parent"]["custom"] == {"recovered_after_crash": True}
    assert reopened.pending_show(pending_id)["state"] == "landed"


def test_runtime_multipart_conflict_retries_prefix_and_suffix_across_reopens(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "multipart-recovery-data"))

    import threading
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from astrid_runtime_client import ApiError
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    source = _media_file(tmp_path / "multipart-target.png")
    timeline.commit(
        lambda t: t.add(source, at=0, hold=3, track="V1", id="multipart-target"),
        label="Seed multipart target", key="multipart-seed",
    )
    path = timeline.session.outbox.path
    entered, release = threading.Event(), threading.Event()
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    _, credential = daemon.credentials.provision("multipart-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="multipart-peer", credential=credential)

    def conflict_after_suffix(project_id, timeline_id, body, **kwargs):
        entered.set()
        if not release.wait(timeout=60):
            raise TimeoutError("multipart suffix was not queued")
        peer = Timeline.open(
            project, "film", client=peer_client, outbox_path=tmp_path / "multipart-peer.json"
        )
        peer_result = peer.commit(
            lambda t: t.bundle["parent"].update({"name": "Disjoint multipart peer"}),
            label="Disjoint multipart peer", key="multipart-peer",
        )
        assert peer_result["receipt"]["receipt_id"]
        raise ApiError(409, "conflict", "simulated retryable conflict", details={
            "status": "conflict", "reason": "injected_conflict",
            "conflicts": [], "current": dict(peer.head),
        })

    monkeypatch.setattr(generated, "commit_timeline", conflict_after_suffix)
    prefix = timeline.session.queue_move("parent/clip:multipart-target", 5)
    outcomes = []

    def send_prefix():
        try:
            timeline.session.commit(
                prefix, idempotency_key="multipart-prefix", label="Multipart prefix"
            )
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=send_prefix)
    worker.start()
    try:
        assert entered.wait(timeout=60)
        suffix_timeline = Timeline.open(project, "film", client=client, outbox_path=path)
        suffix = suffix_timeline.session.queue_move("parent/clip:multipart-target", 7)
    finally:
        release.set()
        worker.join(timeout=60)
    assert not worker.is_alive(), "multipart prefix did not finish after refusal"
    assert len(outcomes) == 1 and isinstance(outcomes[0], CommitConflict), outcomes
    pending_id = outcomes[0].pending_id
    assert pending_id
    saved_packet = json.loads(path.read_text(encoding="utf-8"))["pending_changes"][pending_id]
    assert [part["commands"] for part in saved_packet["parts"]] == [[prefix], [suffix]]

    monkeypatch.setattr(generated, "commit_timeline", original)
    reopened = Timeline.open(project, "film", client=client, outbox_path=path)
    first_preview = reopened.retry(pending_id)
    assert first_preview["status"] == "preview", first_preview
    first = reopened.commit(first_preview)
    assert first["status"] in {"accepted", "merged", "landed", "part_landed"}
    after_prefix = reopened.pending_show(pending_id)
    assert after_prefix["state"] == "pending"
    assert after_prefix["part"] == 1
    assert len(after_prefix["receipts"]) == 1

    reopened = Timeline.open(project, "film", client=client, outbox_path=path)
    second_preview = reopened.retry(pending_id)
    assert second_preview["status"] == "preview"
    second = reopened.commit(second_preview)
    assert second["status"] in {"accepted", "merged", "landed"}
    final = reopened.pending_show(pending_id)
    assert final["state"] == "landed"
    assert len(final["receipts"]) == 2
    assert len(set(final["receipts"])) == 2
    assert reopened.clip("multipart-target").at == pytest.approx(7)


def test_cross_actor_history_recovers_saved_part_during_delayed_ack(
    editing_case, tmp_path, monkeypatch,
):
    daemon, _client, project, setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "cross-actor-part-data"))

    import threading
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from astrid_runtime_client import ApiError
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    source = _media_file(tmp_path / "cross-actor-part.png")
    setup.commit(
        lambda t: t.add(source, at=0, hold=3, track="V1", id="cross-actor-target"),
        label="Seed cross actor target", key="cross-actor-seed",
    )
    _, owner_credential = daemon.credentials.provision("cross-actor-owner", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    _, recovery_credential = daemon.credentials.provision("cross-actor-recovery", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    owner_client = open_client(daemon, actor="cross-actor-owner", credential=owner_credential)
    recovery_client = open_client(
        daemon, actor="cross-actor-recovery", credential=recovery_credential,
    )
    owner = Timeline.open(project, "film", client=owner_client)
    recovery = Timeline.open(project, "film", client=recovery_client)

    entered, release = threading.Event(), threading.Event()
    generated = owner_client._remote._transport._generated
    original = generated.commit_timeline

    def conflict_after_suffix(project_id, timeline_id, body, **kwargs):
        entered.set()
        if not release.wait(timeout=60):
            raise TimeoutError("saved suffix was not queued before the conflict")
        raise ApiError(409, "conflict", "simulated retryable conflict", details={
            "status": "conflict", "reason": "injected_conflict",
            "conflicts": [], "current": dict(owner.head),
        })

    monkeypatch.setattr(generated, "commit_timeline", conflict_after_suffix)
    prefix = owner.session.queue_move("parent/clip:cross-actor-target", 5)
    outcomes = []

    def save_multipart_record():
        try:
            owner.session.commit(prefix, idempotency_key="cross-actor-prefix", label="Cross actor edit")
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=save_multipart_record)
    worker.start()
    try:
        assert entered.wait(timeout=60), "part 0 did not reach the simulated conflict barrier"
        suffix_owner = Timeline.open(project, "film", client=owner_client,
                                     outbox_path=owner.session.outbox.path)
        suffix = suffix_owner.session.queue_move("parent/clip:cross-actor-target", 7)
    finally:
        release.set()
        worker.join(timeout=60)
    assert not worker.is_alive(), "part 0 did not finish saving the dependent suffix"
    assert len(outcomes) == 1 and isinstance(outcomes[0], CommitConflict), outcomes
    pending_id = outcomes[0].pending_id
    assert pending_id
    saved = owner.pending_show(pending_id)
    assert [part["commands"] for part in saved["parts"]] == [[prefix], [suffix]]

    monkeypatch.setattr(generated, "commit_timeline", original)
    preview = owner.retry(pending_id)
    assert preview["status"] == "preview"

    ack_entered, release_ack = threading.Event(), threading.Event()
    sent_parts = []

    def accept_then_delay_ack(project_id, timeline_id, body, **kwargs):
        sent_parts.append(copy.deepcopy(body["commands"]))
        result = original(project_id, timeline_id, body, **kwargs)
        if body["commands"] == [prefix]:
            ack_entered.set()
            if not release_ack.wait(timeout=60):
                raise TimeoutError("history recovery did not release part 0 acknowledgement")
        return result

    monkeypatch.setattr(generated, "commit_timeline", accept_then_delay_ack)
    owner_results = []

    def send_part_zero():
        try:
            owner_results.append(owner.commit(preview))
        except Exception as exc:
            owner_results.append(exc)

    delayed = threading.Thread(target=send_part_zero)
    delayed.start()
    try:
        assert ack_entered.wait(timeout=60), "Runtime did not accept part 0 before the delayed ack"
        recovered_part = recovery.retry(pending_id)
        assert recovered_part["status"] == "part_landed", recovered_part
        assert recovered_part["record"]["part"] == 1
        assert len(recovered_part["record"]["receipts"]) == 1
        assert len(set(recovered_part["record"]["receipts"])) == 1
    finally:
        release_ack.set()
        delayed.join(timeout=60)
    assert not delayed.is_alive(), "delayed part 0 acknowledgement did not finish"
    assert len(owner_results) == 1 and isinstance(owner_results[0], dict), owner_results

    after_ack = recovery.pending_show(pending_id)
    assert after_ack["part"] == 1
    assert len(after_ack["receipts"]) == 1
    assert len(set(after_ack["receipts"])) == 1

    suffix_preview = recovery.retry(pending_id)
    assert suffix_preview["status"] == "preview"
    recovery_generated = recovery_client._remote._transport._generated
    recovery_commit = recovery_generated.commit_timeline

    def count_recovery_suffix(project_id, timeline_id, body, **kwargs):
        sent_parts.append(copy.deepcopy(body["commands"]))
        return recovery_commit(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(recovery_generated, "commit_timeline", count_recovery_suffix)
    suffix_result = recovery.commit(suffix_preview)
    assert suffix_result["status"] in {"accepted", "merged", "landed"}
    final = recovery.pending_show(pending_id)
    assert final["state"] == "landed"
    assert len(final["receipts"]) == 2
    assert len(set(final["receipts"])) == 2
    assert sent_parts.count([suffix]) == 1
    assert recovery.clip("cross-actor-target").at == pytest.approx(7)


def test_same_actor_default_outbox_recovers_captured_reviewed_list_after_process_exit(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, _setup = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "same-actor-list-data"))

    import json
    import multiprocessing
    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    actor = "ux-core-same-actor"
    _, credential = daemon.credentials.provision(actor, [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor=actor, credential=credential)
    peer = Timeline.open(project, "film", client=peer_client)
    initial_head = copy.deepcopy(peer.head)

    context = multiprocessing.get_context("spawn")
    captured_queue, result_queue = context.Queue(), context.Queue()
    release = context.Event()
    process = context.Process(
        target=_same_actor_reviewed_list_process,
        args=(daemon.endpoint, credential, daemon.service.realm["id"], project,
              captured_queue, release, result_queue),
    )
    process.start()
    try:
        captured = captured_queue.get(timeout=60)
        assert captured["base"] == initial_head
        assert captured["commands"]
        assert captured["outbox"] == str(peer.session.outbox.path)

        peer_result = peer.commit(
            lambda t: t.bundle["parent"].update({"name": "Same actor reviewed peer edit"}),
            label="Same actor reviewed peer edit", key="ux-core-reviewed-peer",
        )
        assert peer_result["receipt"]["receipt_id"]
        after_peer = json.loads(peer.session.outbox.path.read_text(encoding="utf-8"))
        assert after_peer["flight"] is None
        assert after_peer["pending"] == []
        assert after_peer["acknowledged"]["ux-core-reviewed-peer"]
        peer_state = {key: copy.deepcopy(value) for key, value in after_peer.items()
                      if key != "pending_changes"}
        release.set()
        result = result_queue.get(timeout=60)
        process.join(timeout=10)
        assert not process.is_alive(), "same-actor reviewed-list child process did not exit"
        assert process.exitcode == 0
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    assert "error" not in result, result
    pending_id = result["pending_id"]
    assert pending_id
    saved = peer.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "list"
    assert saved["parts"][0]["base"] == captured["base"]
    assert saved["parts"][0]["commands"] == captured["commands"]

    after_conflict = json.loads(peer.session.outbox.path.read_text(encoding="utf-8"))
    assert {key: value for key, value in after_conflict.items()
            if key != "pending_changes"} == peer_state
    assert after_conflict["pending_changes"][pending_id]["parts"][0]["commands"] == captured["commands"]

    # The reviewed list is durable; a fresh handle detects its conflict without
    # publishing it, then a newly derived list closes the same saved record.
    recovered = Timeline.open(project, "film", client=peer_client)
    history_before_retry = len(client.timelines.history(project, "film", limit=100).data["items"])
    packet_before_retry = copy.deepcopy(after_conflict["pending_changes"][pending_id])
    fallback = recovered.retry(pending_id)
    assert fallback["status"] == "re_derive"
    assert len(client.timelines.history(project, "film", limit=100).data["items"]) == history_before_retry
    assert recovered.pending_show(pending_id)["state"] == "pending"
    after_retry = json.loads(peer.session.outbox.path.read_text(encoding="utf-8"))
    assert after_retry["pending_changes"][pending_id] == packet_before_retry

    preview = recovered.commit(
        lambda t: t.bundle["parent"].update(
            {"custom": {"same_actor_reviewed_list": True}}),
        label="Same actor reviewed list", dry_run=True,
    )
    assert preview["status"] == "preview"
    assert recovered.head != captured["base"]
    landed = recovered.commit(
        preview["data"]["commands"], label="Same actor reviewed list",
        key="ux-core-reviewed", pending=pending_id,
    )
    assert landed["status"] in {"accepted", "merged", "landed"}
    assert recovered.bundle["parent"]["custom"] == {"same_actor_reviewed_list": True}
    assert recovered.bundle["parent"]["name"] == "Same actor reviewed peer edit"
    history_count = len(client.timelines.history(project, "film", limit=100).data["items"])
    again = recovered.commit(
        preview["data"]["commands"], label="Same actor reviewed list",
        key="ux-core-reviewed", pending=pending_id,
    )
    assert again["status"] in {"accepted", "landed"}
    assert again["receipt"] == landed["receipt"]
    assert len(client.timelines.history(project, "film", limit=100).data["items"]) == history_count
    assert recovered.pending_show(pending_id)["state"] == "landed"
