"""Disposable Runtime-shaped transport for timeline skill subprocess recipes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]


def _append_call(name: str, **fields: Any) -> None:
    path = os.environ.get("ASTRID_SKILL_CLI_CALLS")
    if not path:
        raise RuntimeError("skill CLI fake transport has no call log")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"route": name, **fields}, sort_keys=True) + "\n")


def write_state(path: Path, *, head: str, bundles: Mapping[str, Mapping[str, Any]],
                closures: Mapping[str, Mapping[str, Any]], history: list[Mapping[str, Any]],
                fixture_identity: str) -> str:
    media: dict[str, dict[str, str]] = {}
    for bundle in bundles.values():
        for shot in (bundle.get("shots") or {}).values():
            internal = shot.get("internal_timeline") if isinstance(shot, Mapping) else None
            registry = internal.get("registry") if isinstance(internal, Mapping) else None
            assets = registry.get("assets") if isinstance(registry, Mapping) else None
            for entry in (assets or {}).values() if isinstance(assets, Mapping) else ():
                if not isinstance(entry, Mapping):
                    continue
                media_id = entry.get("media_id") or entry.get("object_id")
                digest = (entry.get("content_sha256") or entry.get("sha256") or
                          entry.get("hash") or media_id)
                if isinstance(media_id, str) and isinstance(digest, str):
                    media[media_id] = {"project_ref": "demo", "project_slug": "demo",
                                       "media_id": media_id, "content_hash": digest}
    state = {
        "head": head,
        "bundles": {key: copy.deepcopy(dict(value)) for key, value in bundles.items()},
        "closures": {key: copy.deepcopy(dict(value)) for key, value in closures.items()},
        "history": [copy.deepcopy(dict(row)) for row in history],
        "media": list(media.values()),
        "fixture_identity": fixture_identity,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_simple_state(path: Path) -> str:
    """Build two local revisions from the tiny publishable authoring fixture."""
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate, open_authoring_bundle
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline.commit import _digest

    fixture = ROOT / "tests/fixtures/timeline_editing/tiny_publishable.json"
    bundle = json.loads(fixture.read_text(encoding="utf-8"))
    bundle["project_id"], bundle["timeline_id"] = "p", "t"
    for shot_id, shot in bundle.get("shots", {}).items():
        for binding in shot.get("payload", {}).get("text_bindings", []):
            if isinstance(binding, dict) and binding.get("content_hash"):
                binding["binding_id"] = f"script-{shot_id}"
                binding["media_id"] = binding["content_hash"]
                binding["byte_size"] = len(str(binding.get("text") or "").encode("utf-8"))
    bundle["base_parent"]["revision_id"] = "rev-0"
    compiled = compile_authoring_candidate(Checkout(copy.deepcopy(bundle)).document()).publication
    base_parent = compiled["parent_composition"]
    closures: dict[str, dict[str, Any]] = {}
    bundles: dict[str, dict[str, Any]] = {}
    for revision in ("rev-0", "rev-old"):
        parent = {"revision_id": revision, "project_id": "p", "timeline_id": "t",
                  "payload": copy.deepcopy(base_parent), "content_digest": _digest(base_parent)}
        closures[revision] = {
            "parent_revision": parent,
            "shot_revisions": [dict(row, project_id="p") for row in compiled["shot_revisions"]],
            "internal_timeline_revisions": [dict(row, project_id="p")
                                            for row in compiled["internal_timeline_revisions"]],
        }
        bundles[revision] = open_authoring_bundle(
            parent, shot_revisions=closures[revision]["shot_revisions"],
            internal_timeline_revisions=closures[revision]["internal_timeline_revisions"],
        )
    history = [
        {"revision_id": "rev-0", "publication_id": "pub-0", "previous_revision_id": "rev-old",
         "previous_publication_id": "pub-old", "is_current_head": True, "created_at": "fixture"},
        {"revision_id": "rev-old", "publication_id": "pub-old", "previous_revision_id": None,
         "previous_publication_id": None, "is_current_head": False, "created_at": "fixture"},
    ]
    return write_state(path, head="rev-0", bundles=bundles, closures=closures, history=history,
                       fixture_identity=f"tiny_publishable.json sha256={hashlib.sha256(fixture.read_bytes()).hexdigest()}")


def install() -> None:
    """Replace launcher acquisition in this child with a local, file-backed fake."""
    from astrid.core.cli.domain_output import DomainResult
    from astrid.sdk.contracts import ErrorObject
    from astrid.sdk.client import AstridClient, _REQUIRED_SCOPES
    from banodoco_workspace_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST

    class FakeTransport:
        def __init__(self) -> None:
            self.state_path = Path(os.environ["ASTRID_SKILL_CLI_STATE"])
            from types import SimpleNamespace
            self.media = SimpleNamespace(list=self.list_media)

        def state(self) -> dict[str, Any]:
            return json.loads(self.state_path.read_text(encoding="utf-8"))

        def health(self) -> dict[str, Any]:
            _append_call("health")
            return {"status": "ok", "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
                    "runtime_epoch": 1, "runtime_instance_id": "skill-cli-fixture",
                    "runtime_session_id": "skill-cli-fixture-session"}

        def handshake(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            _append_call("handshake")
            return {"protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
                    "session_id": "skill-cli-fixture-session", "actor_id": "agent:fixture",
                    "realm_id": "skill-cli-fixture",
                    "scopes": sorted(_REQUIRED_SCOPES), "capabilities": ["execution_binding.targeted.v1"]}

        def list_project_parent_composition_revisions(self, project: str, timeline: str, **_kwargs: Any):
            _append_call("list_project_parent_composition_revisions", project=project, timeline=timeline)
            return copy.deepcopy(self.state()["history"]), None

        def get_project_parent_composition_revision(self, project: str, timeline: str, revision: str):
            _append_call("get_project_parent_composition_revision", project=project, timeline=timeline,
                         revision=revision)
            return copy.deepcopy(self.state()["closures"][revision]["parent_revision"])

        def get_project_shot_revision(self, project: str, shot_id: str, revision: str):
            _append_call("get_project_shot_revision", project=project, shot=shot_id, revision=revision)
            rows = self.state()["closures"]
            return copy.deepcopy(next(row for closure in rows.values() for row in closure["shot_revisions"]
                                      if row.get("shot_id") == shot_id and row.get("revision_id") == revision))

        def get_project_timeline_revision(self, project: str, timeline: str, revision: str):
            _append_call("get_project_timeline_revision", project=project, timeline=timeline, revision=revision)
            rows = self.state()["closures"]
            return copy.deepcopy(next(row for closure in rows.values()
                                      for row in closure["internal_timeline_revisions"]
                                      if row.get("revision_id") == revision))

        def commit_timeline(self, body: Mapping[str, Any]):
            _append_call("commit_timeline", dry_run=bool(body.get("dry_run")), base=body.get("base_revision_id"))
            return {"status": "preview", "request_id": "local-fixture-preview",
                    "base_revision_id": body.get("base_revision_id"), "new_head": self.state()["head"]}

        def get_project(self, project: str):
            _append_call("get_project", project=project)
            return {"project_id": "p", "slug": "demo", "metadata": {"default_timeline_id": "t"}}

        def list_timelines(self, project: str, *, cursor: str | None = None, limit: int = 50):
            state = self.state()
            _append_call("list_timelines", project=project, limit=limit)
            return ([{"timeline_id": "t", "id": "t", "slug": "main",
                      "head_revision_id": state["head"]}], None)

        def list_media(self, project: str, *, cursor: str | None = None, limit: int = 50):
            _append_call("list_media", project=project, limit=limit)
            return (copy.deepcopy(self.state()["media"][:limit]), None)

        def get_object(self, object_id: str):
            _append_call("get_object", object_id=object_id)
            for bundle in self.state()["bundles"].values():
                for shot in bundle.get("shots", {}).values():
                    for binding in shot.get("payload", {}).get("text_bindings", []):
                        if (isinstance(binding, Mapping) and binding.get("media_id") == object_id
                                and isinstance(binding.get("text"), str)):
                            return {"data": binding["text"].encode("utf-8")}
            raise KeyError(object_id)

        def get_project_shot_text_binding(self, project: str, binding_id: str):
            _append_call("get_project_shot_text_binding", project=project, binding_id=binding_id)
            for bundle in self.state()["bundles"].values():
                for shot in bundle.get("shots", {}).values():
                    for binding in shot.get("payload", {}).get("text_bindings", []):
                        if isinstance(binding, Mapping) and binding.get("binding_id") == binding_id:
                            return {"head": binding.get("head"), "content_hash": binding.get("content_hash")}
            raise KeyError(binding_id)

    class FakeTimelines:
        def __init__(self, transport: FakeTransport) -> None:
            self.transport = transport

        def resolve_scope(self, project: str, timeline: str):
            _append_call("resolve_scope", project=project, timeline=timeline)
            return DomainResult.success({"project_id": "p", "timeline_id": "t"})

        def open_composition(self, project: str, timeline: str, *, revision_id: str | None = None,
                             **_kwargs: Any):
            state = self.transport.state()
            revision = revision_id or state["head"]
            _append_call("open_composition", project=project, timeline=timeline, revision=revision)
            if revision not in state["bundles"]:
                return DomainResult.failure(ErrorObject("not_found", "fixture revision missing", {}))
            return DomainResult.success({
                "summary": {"revision_id": revision, "head_revision_id": state["head"]},
                "native_inspection": {"project_id": "p", "timeline_id": "t",
                                      "head_revision_id": state["head"]},
            })

        def open_bundle(self, project: str, timeline: str, *, revision_id: str | None = None):
            state = self.transport.state()
            revision = revision_id or state["head"]
            _append_call("open_bundle", project=project, timeline=timeline, revision=revision)
            bundle = state["bundles"].get(revision)
            if bundle is None:
                return DomainResult.failure(ErrorObject("not_found", "fixture revision missing", {}))
            return DomainResult.success({"bundle": copy.deepcopy(bundle), "project_id": "p", "timeline_id": "t",
                                         "revision_id": revision, "is_current_head": revision == state["head"]})

        def head_canonical(self, project: str, timeline: str):
            state = self.transport.state()
            _append_call("head_canonical", project=project, timeline=timeline, revision=state["head"])
            return DomainResult.success({"revision_id": state["head"], "publication_id": None, "updated_at": None})

        def list_parent_composition_revisions(self, project: str, timeline: str,
                                              *, cursor: str | None = None, limit: int = 50):
            state = self.transport.state()
            _append_call("list_parent_composition_revisions", project=project, timeline=timeline, limit=limit)
            return DomainResult.success({"items": copy.deepcopy(state["history"][:limit]), "next_cursor": None})

        def restore_parent_composition_revision(self, project: str, timeline: str, revision: str,
                                                *, expected_head: str | None,
                                                idempotency_key: str | None = None):
            _append_call("restore_parent_composition_revision", project=project, timeline=timeline,
                         revision=revision, expected_head=expected_head, idempotency_key=idempotency_key)
            return DomainResult.success({"status": "accepted", "restored_revision_id": revision,
                                         "expected_head": expected_head})

    class FakeClient:
        def __init__(self) -> None:
            self._transport = FakeTransport()
            self.timelines = FakeTimelines(self._transport)

        def __enter__(self):
            _append_call("client_open")
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

    class Context:
        def __enter__(self):
            self.client = FakeClient()
            return self.client.__enter__()

        def __exit__(self, *args: Any) -> bool:
            return self.client.__exit__(*args)

    AstridClient.open_from_launcher = classmethod(lambda cls, **_kwargs: Context())
    AstridClient.open_existing_from_launcher = classmethod(lambda cls, **_kwargs: Context())

    from astrid.core.timeline import scene_answers
    from astrid.sdk.timeline.checkout import Checkout

    class SyntheticSceneAnswers:
        def query_scene(self, *, revision_digest: str, scene: str, frames: list[int]):
            _append_call("synthetic_scene_answers", frame_count=len(frames))
            return [dict(frame=frame, coverage="COVERED", content="CONTENT", findings=[], layers=[])
                    for frame in frames]

    scene_answers._client = lambda: SyntheticSceneAnswers()
    Checkout._native_outline_integrity = lambda *_args, **_kwargs: None

    if os.environ.get("ASTRID_SKILL_CLI_FAST_WATCH") == "1":
        from types import SimpleNamespace
        from astrid.packs.timeline import cli_sync

        ticks = iter((100.0, 100.0, 3701.0))
        cli_sync.time = SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _seconds: None)
