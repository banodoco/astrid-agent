"""Current requester seam for registered native assembly admissions."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_sync import Client, Generated

DIGEST = "sha256:" + "a" * 64


def _requester(tmp_path):
    generated = Generated(tmp_path / "outbox.json")
    client = Client(generated)
    selectors, associations, media, verified = [], {}, {}, {}
    for index in range(2):
        object_id = "sha256:" + str(index + 1) * 64
        selector = {"association_id": f"assoc-{index}", "object_id": object_id, "version": 1}
        selectors.append(selector)
        associations[selector["association_id"]] = {
            **selector, "task_id": f"child-{index}", "project_id": "p", "durability": "durable",
            "role": "primary", "lifecycle": {"state": "available", "version": 1},
            "media_type": "video/mp4", "size": 12,
        }
        pin = {"asset": f"v-{index}", "generation_id": f"g-{index}",
               "variant_id": f"v-{index}", "object_id": object_id, "catalog_digest": DIGEST}
        media[selector["association_id"]] = pin
        verified[pin["variant_id"]] = {"selection": pin, "media_type": "video/mp4",
                                        "size": 12, "duration_seconds": index + 1.0}

    def get_association(association_id):
        return copy.deepcopy(associations[association_id])

    def get_selection(_project, selector):
        return copy.deepcopy(media[selector["association_id"]])

    def get_verified(_project, selection):
        return copy.deepcopy(verified[selection["variant_id"]])

    generated.get_managed_output = get_association
    generated.get_project_media_selection = get_selection
    generated.get_project_verified_media = get_verified
    transport = client._remote._transport
    transport.get_managed_output = get_association
    transport.get_project_media_selection = get_selection
    transport.get_project_verified_media = get_verified
    calls = []

    def prepare(**body):
        return DomainResult.success({
            "capability_id": body["capability"], "capability_digest": body["capability_digest"],
            "project_id": body["project_id"], "schema_version": "1",
            "spec": body["spec"], "input_object_ids": body["input_manifest"],
            "storage_estimate": body["storage_estimate"], "idempotency_key": body["idempotency_key"],
        })

    def dispatch(arguments):
        flight = json.loads(generated.path.read_bytes())["flight"]
        assert flight["kind"] == "assembly-admission"
        assert flight["key"] == arguments["idempotency_key"]
        calls.append(copy.deepcopy(arguments))
        return DomainResult.success({"task_id": "assembler"})

    client.tasks = SimpleNamespace(prepare_admission=prepare, dispatch_admission=dispatch)
    session = TimelineSession.open("p", "t", client=client, outbox_path=generated.path)
    return session, generated, selectors, associations, calls


def _admit(session, selectors):
    return session.admit_assembly(
        selectors,
        replace_clips=["parent/clip:a", "parent/clip:b"],
        assembler_digest=DIGEST,
        idempotency_key="assembly",
    )


def test_admission_freezes_original_target_and_ordered_associations(tmp_path):
    session, generated, selectors, _associations, calls = _requester(tmp_path)
    generated.head = {"revision_id": "peer", "publication_id": "peer-publication"}

    assert _admit(session, selectors)["data"]["task_id"] == "assembler"
    body, = calls
    capture = body["spec"]["inputs"]["resolved_children"]
    target = {"project_id": "p", "timeline_id": "t", "base_revision_id": "r0",
              "base_publication_id": "pub0"}
    assert capture["admitted_target"] == target
    assert body["spec"]["runtime_dependencies"]["publication"]["admitted_target"] == target
    assert [child["association"] for child in capture["children"]] == selectors
    assert body["spec"]["runtime_dependencies"]["aggregation"]["order"] == [
        selector["object_id"] for selector in selectors
    ]
    assert generated.path.read_bytes()


def test_lost_admission_ack_reuses_durable_body_without_recapture(tmp_path, monkeypatch):
    session, generated, selectors, _associations, calls = _requester(tmp_path)
    dispatch = session.client.tasks.dispatch_admission

    def lose_ack(arguments):
        dispatch(arguments)
        raise WorkspaceClientError(0, "transport_error", "lost admission acknowledgement")

    session.client.tasks.dispatch_admission = lose_ack
    with pytest.raises(WorkspaceClientError):
        _admit(session, selectors)
    saved = generated.path.read_bytes()
    original = copy.deepcopy(calls[0])

    def no_recapture(*_args, **_kwargs):
        raise AssertionError("admission recovery reread mutable inputs")

    monkeypatch.setattr(session.client.tasks, "prepare_admission", no_recapture)
    monkeypatch.setattr(session.client._remote._transport, "get_managed_output", no_recapture)
    monkeypatch.setattr(session.client._remote._transport, "get_project_media_selection", no_recapture)
    monkeypatch.setattr(session.client._remote._transport, "get_project_verified_media", no_recapture)
    restarted = TimelineSession.open("p", "t", client=session.client, outbox_path=generated.path)
    restarted.client.tasks.dispatch_admission = dispatch
    assert _admit(restarted, selectors)["data"]["task_id"] == "assembler"
    assert calls == [original, original]
    assert json.loads(saved)["flight"]["kind"] == "assembly-admission"
