"""Seed a saved parent closure into a real Runtime through its host commit store.

``publish_parent_composition`` is retired, so a fixture that needs a stored parent chain accepts the
closure at Runtime's test acceptance boundary instead (the same trusted setup Runtime's own tests use).
This is TEST SETUP, not Agent semantic acceptance: no Agent command compiled these revisions.
"""
from __future__ import annotations

import copy
from typing import Any

from tests.helpers.runtime import use_runtime

PUBLISHER = "trusted-test-setup"


def _media(*payloads: Any) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"media_id", "object_id"} and isinstance(child, str):
                    media_id = child if child.startswith("sha256:") else "sha256:" + child
                    if len(media_id) == len("sha256:") + 64:
                        found[media_id] = {"media_id": media_id, "content_digest": media_id}
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(list(payloads))
    return [found[key] for key in sorted(found)]


def _closure(publication: dict, current: dict, digest) -> dict:
    parent_payload = copy.deepcopy(publication["parent_composition"])
    parent = {"revision_id": publication["parent_revision_id"],
              "content_digest": digest(parent_payload), "payload": parent_payload}
    wanted = {(row["shot_id"], row["shot_revision_id"]) for row in parent_payload["occurrences"]}
    shots = []
    shot_sources = {(row["shot_id"], row["revision_id"]): row
                    for row in [*current["shot_revisions"], *publication.get("shot_revisions", [])]}
    for source in shot_sources.values():
        if (source["shot_id"], source["revision_id"]) not in wanted:
            continue
        payload = copy.deepcopy(source["payload"])
        payload["internal_timeline_revision_id"] = source["internal_timeline_revision_id"]
        shots.append({"shot_id": source["shot_id"], "revision_id": source["revision_id"],
                      "internal_timeline_revision_id": source["internal_timeline_revision_id"],
                      "content_digest": digest(payload), "payload": payload})
    wanted_internals = {row["internal_timeline_revision_id"] for row in shots}
    internals = []
    internal_sources = {row["revision_id"]: row for row in [
        *current["internal_timeline_revisions"], *publication.get("internal_timeline_revisions", [])]}
    for source in internal_sources.values():
        if source["revision_id"] in wanted_internals:
            payload = copy.deepcopy(source["payload"])
            internals.append({"timeline_id": source["timeline_id"], "revision_id": source["revision_id"],
                              "content_digest": digest(payload), "payload": payload})
    shots.sort(key=lambda row: (row["shot_id"], row["revision_id"]))
    internals.sort(key=lambda row: (row["timeline_id"], row["revision_id"]))
    manifest = {
        "shots": [{key: row[key] for key in ("shot_id", "revision_id", "internal_timeline_revision_id", "content_digest")}
                  for row in shots],
        "internal_timelines": [{key: row[key] for key in ("timeline_id", "revision_id", "content_digest")}
                               for row in internals],
        "media": _media(parent["payload"], *(row["payload"] for row in [*shots, *internals])),
        "documents": [],
    }
    return {"schema_version": 1, "project_id": publication["project_id"], "timeline_id": publication["timeline_id"],
            "parent_revision": parent, "shot_revisions": shots, "internal_timeline_revisions": internals,
            "dependency_manifest": manifest}


def seed_trusted_parent(service: Any, publication: dict, *, idempotency_key: str) -> dict:
    """Accept ``publication`` (the retired publisher's body) as the next head of its timeline."""
    use_runtime()
    from runtime_protocol.timeline_commit_store import digest, request_identity

    project, timeline = publication["project_id"], publication["timeline_id"]
    commits, _ = service._timeline_commits()
    authorize = lambda: None  # noqa: E731
    commits.bootstrap(project, timeline, authorize=authorize)
    head = commits.head(project, timeline)
    if publication.get("expected_head") is not None:
        assert publication["expected_head"] == head["revision_id"], "seed chain must extend the current head"
    current = commits.revision_closure(project, timeline, head["revision_id"])
    document = _closure(publication, current, digest)
    before = {key: current["parent_revision"][key] for key in ("revision_id", "content_digest")}
    after = {key: document["parent_revision"][key] for key in ("revision_id", "content_digest")}
    command = {"schema_version": 1, "op": "clip.move", "version": "1", "select": "parent/clip:seed",
               "args": {"on": 0}, "created": []}
    request = {"api_version": 1, "project_id": project, "timeline_id": timeline,
               "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
               "commands": [command], "rationale": "Trusted TEST SETUP for a saved parent; not Agent semantic acceptance"}
    context = {"request": request,
               "invocation": {"operation": "commitTimeline", "path": f"/v1/projects/{project}/timelines/{timeline}/commit",
                              "body": copy.deepcopy(request)},
               "idempotency_key": idempotency_key, "realm_id": service.realm["id"], "publisher": PUBLISHER}
    context["request_hash"] = request_identity(context)
    record = {"ordinal": 0, "command": copy.deepcopy(command),
              "writes": [{"ref": "timeline", "path": [], "before": {"state": "present", "value": before},
                          "after": {"state": "present", "value": after}}],
              "reads": [{"ref": "timeline", "path": [], "state": {"state": "present", "value": before}}],
              "selections": [], "bindings": [], "created": [], "dependencies": [], "derived_writes": []}
    value = {"expected_head": head, "canonical_document": document, "document_digest": digest(document),
             "records": {"schema_version": 1, "commands": [record], "undo_of": [], "restored_from": None},
             "id_map": {}, "semantic_events": [], "provider_version": "trusted-test-setup-1",
             "compiler_version": "trusted-test-setup-1"}
    return commits.accept(context, value, authorize=authorize)
