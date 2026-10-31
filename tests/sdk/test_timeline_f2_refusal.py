"""Runtime refusals stay definitive and preserve the exact attempted request."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.sdk import timeline
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.sync import CommitConflict, CommitRefused
from astrid_runtime_client import ApiError
from tests.sdk import test_timeline_sync as sync_test
from tests.sdk.test_timeline_sync import connected as connected_fixture  # noqa: F401

pytest_plugins = ("tests.sdk.test_timeline_editing_acceptance_runtime",)


def test_runtime_refusal_is_not_retried_or_reclassified_and_saves_verified_payload(
    connected_fixture, monkeypatch, tmp_path,
):  # noqa: F811
    client, generated, path = connected_fixture
    editor = timeline.open("p", "t", client=client, outbox_path=path)

    source = tmp_path / "refused.png"
    payload = b"refused media"
    source.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    pin = {
        "asset": "refused-asset",
        "object_id": digest,
        "generation_id": "refused-generation",
        "variant_id": "refused-variant",
        "catalog_digest": "sha256:" + "b" * 64,
    }
    verified = {
        "selection": copy.deepcopy(pin),
        "media_type": "image/png",
        "size": len(payload),
        "width": 1,
        "height": 1,
    }
    import_record = {
        "object_id": digest,
        "generation_id": pin["generation_id"],
        "variant_id": pin["variant_id"],
        "catalog_write": True,
        "retained_on_undo": True,
    }
    monkeypatch.setattr("astrid.sdk.authoring_media._metadata", lambda _path: {
        "digest": digest,
        "media_type": "image/png",
        "filename": source.name,
        "width": 1,
        "height": 1,
    })
    monkeypatch.setattr(
        "astrid.sdk.timeline.media_add._selection",
        lambda *_args: (copy.deepcopy(pin), copy.deepcopy(verified)),
    )
    execute = sync_test.execute
    monkeypatch.setattr(
        sync_test,
        "execute",
        lambda bundle, commands, **kwargs: execute(
            bundle, commands, verified_media=[verified], **kwargs,
        ),
    )

    # The 409 envelope and advanced current head must not turn this definitive
    # authority refusal into a retryable conflict.
    generated.refusal = ApiError(409, "conflict", "authority revoked", details={
        "status": "refused",
        "reason": "authority_revoked",
        "current": {"revision_id": "r1", "publication_id": "pub1"},
    })
    evaluations = []

    def edit(working):
        evaluations.append(True)
        working.add(source, at=0, hold=2, id="refused-clip")

    with pytest.raises(CommitRefused) as caught:
        editor.commit(edit, idempotency_key="refused-media-edit")

    assert caught.value.failure["details"]["status"] == "refused"
    assert caught.value.pending_id
    assert len(evaluations) == 1
    assert len(generated.calls) == 1

    sent_body, sent_key = generated.calls[0]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pending"] == [] and saved["flight"] is None
    assert "conflict" not in saved and "rejected" not in saved
    rejected = saved["pending_changes"][caught.value.pending_id]
    assert rejected["state"] == "pending"
    assert rejected["origin"] == "function"
    assert rejected["failure"] == caught.value.failure
    assert rejected["clash"]["pending_id"] == rejected["id"]
    assert rejected["clash"]["runtime"]["reason"] == "authority_revoked"
    packet = rejected["parts"][0]
    assert rejected["flight"]["body"].encode() == sent_body
    assert rejected["flight"]["key"] == sent_key == "refused-media-edit"
    assert rejected["flight"]["imports"] == [import_record]
    assert packet["imports"] == [import_record]
    assert packet["evaluation"]["media_cache"][digest]["imports"] == [import_record]
    assert rejected["parts"][0]["evaluation"]["count"] == 1

    assert packet["base"] == {"revision_id": "r0", "publication_id": "pub0"}
    attempted = json.loads(rejected["flight"]["body"])
    assert attempted["base_revision_id"] == "r0"
    assert attempted["base_publication_id"] == "pub0"
    assert packet["commands"] == attempted["commands"]
    assert any(command.get("args", {}).get("media") == pin for command in attempted["commands"])
    assert caught.value.clash == rejected["clash"]


def test_explicit_retry_of_exhausted_function_reruns_saved_function(
    connected_fixture, monkeypatch,
):  # noqa: F811
    client, generated, path = connected_fixture

    def authenticated_bundle(bundle, revision_id):
        compiled = sync_test.compile_authoring_candidate(bundle).publication
        document = {
            "schema_version": 1,
            "project_id": bundle["project_id"],
            "timeline_id": bundle["timeline_id"],
            "parent_revision": {
                "revision_id": revision_id,
                "content_digest": compiled["content_digest"],
                "payload": compiled["parent_composition"],
            },
            "shot_revisions": compiled["shot_revisions"],
            "internal_timeline_revisions": compiled["internal_timeline_revisions"],
            "dependency_manifest": {
                **compiled["dependency_manifest"], "documents": [],
            },
        }
        return bundle_from_document(document)

    # open_bundle(revision_id, publication_id) is the authenticated Runtime
    # read used by fetch_bundle. Keep the fake's initial and advancing heads on
    # that same contract so an explicit function retry can reevaluate them.
    generated.bundles["r0"] = authenticated_bundle(generated.bundles["r0"], "r0")
    editor = timeline.open("p", "t", client=client, outbox_path=path)
    evaluations, sent = [], []
    commit_timeline = generated.commit_timeline
    history = []

    def timeline_history(*_args, **_kwargs):
        from astrid.sdk.contracts import DomainResult

        return DomainResult.success({
            "head": copy.deepcopy(generated.head),
            "items": copy.deepcopy(history),
            "next_cursor": None,
        })

    monkeypatch.setattr(client.timelines, "history", timeline_history)
    # These synthetic head advances have no provider-authored receipt records;
    # overlap behavior is covered against real history in the runtime tests.
    monkeypatch.setattr(
        "astrid.sdk.timeline.commit_session._function_rerun_conflict",
        lambda *_args: None,
    )

    def refuse_three_times(*args, **kwargs):
        sent.append(kwargs["idempotency_key"])
        if len(sent) <= 3:
            # Each conflict has a verified, advancing head, exhausting automatic
            # function retries while leaving an explicit retry available.
            ordinal = len(sent)
            previous = copy.deepcopy(generated.head)
            base = generated.bundles[generated.head["revision_id"]]
            command = {"schema_version": 1, "op": "clip.move", "version": "1",
                       "select": "parent/clip:a", "args": {"on": ordinal}}
            revision = f"retry-head-{ordinal}"
            plan = sync_test.execute(base, [command])
            generated.bundles[revision] = authenticated_bundle(plan.bundle, revision)
            generated.head = {"revision_id": revision, "publication_id": f"pub-{revision}"}
            generated.publications[revision] = generated.head["publication_id"]
            history.append({
                "revision_id": revision,
                "publication_id": generated.head["publication_id"],
                "previous": previous,
            })
            raise ApiError(409, "conflict", "head advanced", details={
                "status": "conflict", "reason": "guard_changed",
                "current": copy.deepcopy(generated.head),
            })
        return commit_timeline(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", refuse_three_times)

    def edit(working):
        evaluations.append(working.bundle["parent"]["clips"][0]["at"])
        working.move("parent/clip:a", evaluations[-1] + 1)

    with pytest.raises(CommitConflict) as exhausted:
        editor.commit(edit)

    assert exhausted.value.pending_id
    assert evaluations == [0, 1, 2]
    record = json.loads(path.read_text(encoding="utf-8"))["pending_changes"][exhausted.value.pending_id]
    assert record["origin"] == "function"
    assert record["parts"][0]["evaluation"]["count"] == 3

    result = editor.retry(exhausted.value)

    assert result["status"] == "accepted"
    assert evaluations == [0, 1, 2, 3]
    assert len(sent) == 4
    assert editor.bundle["parent"]["clips"][0]["at"] == 4
    assert editor.pending_show(exhausted.value.pending_id)["state"] == "landed"


@pytest.mark.parametrize("status", ["refused", "invalid"])
def test_list_origin_runtime_refusal_suggests_fix_retry_or_drop(
    connected_fixture, status,
):  # noqa: F811
    client, generated, path = connected_fixture
    editor = timeline.open("p", "t", client=client, outbox_path=path)
    commands = editor.commit(lambda working: working.move("parent/clip:a", 4), dry_run=True)["data"]["commands"]
    generated.refusal = ApiError(409, status, "edit is not allowed", details={
        "status": status, "reason": "policy",
    })

    with pytest.raises(CommitRefused) as caught:
        editor.commit(commands, label="Reviewed move", idempotency_key="reviewed-move")

    assert caught.value.failure["details"]["status"] == status
    assert caught.value.pending_id
    assert all(word in caught.value.fix.lower() for word in ("fix", "retry", "drop"))
    state = json.loads(path.read_text(encoding="utf-8"))
    record = state["pending_changes"][caught.value.pending_id]
    assert record["origin"] == "list"
    assert record["parts"][0]["commands"] == commands
    assert record["failure"] == caught.value.failure


@pytest.mark.parametrize("status", ["refused", "invalid"])
def test_runtime_non_conflict_result_keeps_callable_for_explicit_retry(
    editing_case, monkeypatch, status,
):
    _daemon, client, project, editor = editing_case
    from astrid.sdk.timeline.sync import CommitRefused

    generated = client._remote._transport._generated
    commit = generated.commit_timeline
    history_before = len(client.timelines.history(project, "film", limit=100).data["items"])
    evaluations = []
    injected = []

    def refuse_once(*args, **kwargs):
        if not injected:
            injected.append(True)
            raise ApiError(422, "invalid", "injected Runtime validation result", details={
                "status": status, "reason": "validation", "issues": ["injected"],
            })
        return commit(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", refuse_once)

    def edit(working):
        evaluations.append(True)
        working.bundle["parent"].update({"name": "Retry after validation"})

    with pytest.raises(CommitRefused) as caught:
        editor.commit(edit, label="Retry after validation", key="runtime-refusal-retry")

    assert len(evaluations) == 1
    assert caught.value.retry_fn is edit
    assert caught.value.failure["details"]["status"] == status
    pending_id = caught.value.pending_id
    saved = editor.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["failure"] == caught.value.failure

    result = editor.retry(caught.value)

    assert result["status"] in {"accepted", "merged", "rebased"}
    assert evaluations == [True, True]
    assert editor.pending_show(pending_id)["state"] == "landed"
    assert len(client.timelines.history(project, "film", limit=100).data["items"]) == history_before + 1


def test_runtime_refusal_retry_by_id_after_restart_explains_callable_requirement(
    editing_case, monkeypatch,
):
    _daemon, client, project, editor = editing_case
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitRefused

    generated = client._remote._transport._generated
    commit = generated.commit_timeline
    history_before = len(client.timelines.history(project, "film", limit=100).data["items"])
    injected = []

    def refuse_once(*args, **kwargs):
        if not injected:
            injected.append(True)
            raise ApiError(422, "invalid", "injected Runtime validation result", details={
                "status": "invalid", "reason": "validation", "issues": ["injected"],
            })
        return commit(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", refuse_once)
    evaluations = []

    def edit(working):
        evaluations.append(True)
        working.bundle["parent"].update({"name": "Retry after restart"})

    with pytest.raises(CommitRefused) as caught:
        editor.commit(edit, label="Retry after restart", key="runtime-restart-retry")

    pending_id = caught.value.pending_id
    reopened = Timeline.open(
        project, "film", client=client, outbox_path=editor.session.outbox.path,
    )
    response = reopened.retry(pending_id)

    assert response["status"] == "refused"
    assert response["pending_id"] == pending_id
    assert "original edit function is needed" in response["summary"]
    assert response["next"] == f"tl.retry({pending_id!r}, your_function)"
    assert evaluations == [True]
    assert reopened.pending_show(pending_id)["state"] == "pending"
    assert len(client.timelines.history(project, "film", limit=100).data["items"]) == history_before
