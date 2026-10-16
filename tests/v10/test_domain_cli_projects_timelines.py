"""Product CLI family tests: projects and timelines (m4 plan steps 25–26).

Task T27 (plan step 25) proves the ``projects`` product family
(``astrid/core/cli/domain_projects.py``): every verb makes **exactly one
SDK call**, returns exact envelopes and keys, persists ``select`` as the
non-authoritative preference, and provides executable help.

Task T28 (plan step 26) proves the ``timelines`` product family
(``astrid/packs/timeline/cli.py``): the planned timeline verbs are
reachable through one-call SDK adapters, legacy aliases and
migration/push/pull/sync/audit/erase/repair are absent, ``copy`` is
absent (deferred past m6), all help is executable, and the gateway
dispatch routes every timelines verb through the product boundary
(m6 teardown removed the legacy timeline CLI and its fallback).

Task T29 (plan step 26 nested shots) proves the manifest-declared nested
``shots`` mount (``astrid/packs/shots/cli.py``): shot
``list/create/add/remove/reorder`` are executable **only** beneath
``timelines`` (``astrid timelines shots <verb>``), each verb is one SDK
call routed through the shot service, there is **no top-level shots
family**, and the canonical entrypoint behavior is retained (the product
parser installs no entrypoint guard; see ``tests/test_canonical_entrypoint.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrid.core.cli.domain_product import run_product_family
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.results import InvocationResult

ENVELOPE_KEYS = {"ok", "data", "error", "receipt", "idempotency_key"}


def _receipt(command_kind: str, key: str) -> CommandReceipt:
    return CommandReceipt(
        receipt_id=f"R-{command_kind}",
        command_kind=command_kind,
        idempotency_key=key,
        request_hash="hash",
        project_id="P-1",
        project_seq=(1, 1),
        event_ids=("E-1",),
        result={"ok": True},
        created_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Recording fake client (one call per service method, canned envelopes)
# ---------------------------------------------------------------------------


class _RecordingProjects:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def create(self, *, slug, name, metadata=None, idempotency_key=None):
        self._owner.calls.append(
            (
                "projects.create",
                {
                    "slug": slug,
                    "name": name,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"slug": slug, "name": name, "project_id": "P-1"},
            receipt=_receipt("project.create", key),
            idempotency_key=key,
        )

    def list(self):
        self._owner.calls.append(("projects.list", {}))
        return DomainResult.success([{"slug": "demo", "name": "Demo"}])

    def show(self, ref):
        self._owner.calls.append(("projects.show", {"ref": ref}))
        return DomainResult.success({"slug": ref, "name": "Demo", "project_id": "P-1"})

    def update(self, ref, *, name=None, metadata=None, idempotency_key=None):
        self._owner.calls.append(
            (
                "projects.update",
                {
                    "ref": ref,
                    "name": name,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"slug": ref, "name": name or "Demo"},
            receipt=_receipt("project.update", key),
            idempotency_key=key,
        )

    def select(self, ref, *, scope="workspace", cwd=None):
        self._owner.calls.append(("projects.select", {"ref": ref, "scope": scope, "cwd": cwd}))
        return DomainResult.success({"slug": ref, "name": "Demo", "project_id": "P-1"})

    def current(self, *, cwd=None):
        self._owner.calls.append(("projects.current", {"cwd": cwd}))
        return DomainResult.success(
            {
                "project": {"slug": "demo", "name": "Demo", "project_id": "P-1"},
                "selection": {"ref": "demo", "scope": "workspace", "path": "/tmp/.astrid/config.json"},
            }
        )

class _RecordingTimelines:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def create(
        self,
        *,
        project,
        slug,
        name,
        config=None,
        registry=None,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "timelines.create",
                {
                    "project": project,
                    "slug": slug,
                    "name": name,
                    "config": config,
                    "registry": registry,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"slug": slug, "name": name, "timeline_id": "T-1"},
            receipt=_receipt("timeline.create", key),
            idempotency_key=key,
        )

    def list(self, project, *, include_archived=False):
        kwargs = {"project": project}
        if include_archived:
            kwargs["include_archived"] = True
        self._owner.calls.append(("timelines.list", kwargs))
        return DomainResult.success([{"slug": "main", "name": "Main"}])

    def show(self, project, ref):
        self._owner.calls.append(("timelines.show", {"project": project, "ref": ref}))
        return DomainResult.success({"slug": ref, "name": "Main", "timeline_id": "T-1"})

    def open_composition(self, project, ref, **kwargs):
        self._owner.calls.append(("timelines.open_composition", {"project": project, "ref": ref, **kwargs}))
        return DomainResult.success({
            "kind": "timeline-inspection",
            "summary": {"authority": "canonical_head", "revision_id": "head-1", "is_current_head": True},
            "query": {}, "targets": [], "clips": [],
        })

    def visualize(self, project, ref, *, mode="auto", options=None, revision_id=None, out=None):
        return self._owner.invoke_result(
            "rendering.timeline_visualize",
            kind="executor", project=project,
            inputs={**dict(options or {}), **({"revision_id": revision_id} if revision_id else {})}, out=out, wait=True,
        )

    def save(
        self,
        project,
        ref,
        *,
        config,
        registry,
        expected_version,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "timelines.save",
                {
                    "project": project,
                    "ref": ref,
                    "config": config,
                    "registry": registry,
                    "expected_version": expected_version,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"slug": ref, "timeline_id": "T-1", "version": expected_version + 1},
            receipt=_receipt("timeline.save", key),
            idempotency_key=key,
        )

    def archive(self, project, ref, *, idempotency_key=None):
        self._owner.calls.append(
            (
                "timelines.archive",
                {"project": project, "ref": ref, "idempotency_key": idempotency_key},
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"slug": ref, "timeline_id": "T-1", "archived": True},
            receipt=_receipt("timeline.archive", key),
            idempotency_key=key,
        )

    def recover(self, project, ref, *, idempotency_key=None):
        self._owner.calls.append(
            (
                "timelines.recover",
                {"project": project, "ref": ref, "idempotency_key": idempotency_key},
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"timeline_id": "T-1", "status": "active", "changed": True},
            receipt=_receipt("timeline.recover", key),
            idempotency_key=key,
        )

    def history(self, project, ref):
        self._owner.calls.append(("timelines.history", {"project": project, "ref": ref}))
        return DomainResult.success([{"event": "timeline.created", "version": 1}])

    def diff(self, project, ref, **versions):
        self._owner.calls.append(("timelines.diff", {"project": project, "ref": ref, **versions}))
        return DomainResult.success([{"version": 1, "changes": {}}])


class _RecordingShots:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def list(self, project):
        self._owner.calls.append(("shots.list", {"project": project}))
        return DomainResult.success([{"id": "S-1", "name": "Opening"}])

    def show(self, project, shot_id):
        self._owner.calls.append(("shots.show", {"project": project, "shot_id": shot_id}))
        return DomainResult.success(
            {
                "id": shot_id,
                "name": "Opening",
                "items": [
                    {
                        "id": "I-1",
                        "media_id": "M-1",
                        "position": 0,
                        "media": {
                            "name": "opening.png",
                            "path": "/tmp/opening.png",
                        },
                    }
                ],
            }
        )

    def create(self, *, project, name, metadata=None, idempotency_key=None):
        self._owner.calls.append(
            (
                "shots.create",
                {
                    "project": project,
                    "name": name,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"id": "S-1", "project_id": "P-1", "name": name},
            receipt=_receipt("shot.create", key),
            idempotency_key=key,
        )

    def add_item(
        self,
        project,
        shot_id,
        *,
        media_id,
        position=None,
        source_frame=None,
        metadata=None,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "shots.add_item",
                {
                    "project": project,
                    "shot_id": shot_id,
                    "media_id": media_id,
                    "position": position,
                    "source_frame": source_frame,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"id": "S-1", "items": [{"item_id": "I-1", "media_id": media_id}]},
            receipt=_receipt("shot.add_item", key),
            idempotency_key=key,
        )

    def remove_item(self, project, shot_id, item_id, *, idempotency_key=None):
        self._owner.calls.append(
            (
                "shots.remove_item",
                {
                    "project": project,
                    "shot_id": shot_id,
                    "item_id": item_id,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"id": "S-1", "items": []},
            receipt=_receipt("shot.remove_item", key),
            idempotency_key=key,
        )

    def reorder(self, project, shot_id, item_ids, *, idempotency_key=None):
        self._owner.calls.append(
            (
                "shots.reorder",
                {
                    "project": project,
                    "shot_id": shot_id,
                    "item_ids": list(item_ids),
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"id": "S-1", "items": [{"item_id": i} for i in item_ids]},
            receipt=_receipt("shot.reorder", key),
            idempotency_key=key,
        )


class _FakeClient:
    """Minimal AstridClient stand-in: records every SDK call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.projects = _RecordingProjects(self)
        self.timelines = _RecordingTimelines(self)
        self.shots = _RecordingShots(self)

    def invoke_result(self, capability_id, **kwargs):
        self.calls.append(("invoke_result", {"capability_id": capability_id, **kwargs}))
        return InvocationResult(
            capability_id=capability_id,
            capability_type="executor",
            native_kind="executor",
            ok=True,
            run_id="01RUN",
            kernel_run_id="R-1",
            kernel_task_id="T-1",
            kernel_attempt_id="A-1",
            manifest_path="/tmp/manifest.json",
            outputs={"artifacts": ["/tmp/manifest.json"]},
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(family: str, args: list[str], client: _FakeClient | None = None) -> int:
    return run_product_family(family, args, client=client or _FakeClient())


def _subparser_choices(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("parser has no subparsers")


# ---------------------------------------------------------------------------
# T27 — projects family
# ---------------------------------------------------------------------------


def test_projects_parser_has_exactly_six_verbs() -> None:
    from astrid.core.cli.domain_projects import COMMANDS, build_parser

    assert tuple(spec.name for spec in COMMANDS) == (
        "create",
        "list",
        "show",
        "update",
        "select",
        "current",
    )
    assert _subparser_choices(build_parser(_FakeClient())) == {
        "create",
        "list",
        "show",
        "update",
        "select",
        "current",
    }


def test_projects_create_is_one_sdk_call_with_exact_envelope(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "projects",
        ["create", "demo", "--name", "Demo", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "projects.create",
            {
                "slug": "demo",
                "name": "Demo",
                "metadata": None,
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"]["slug"] == "demo"
    assert envelope["receipt"]["command_kind"] == "project.create"
    assert envelope["idempotency_key"] == "generated-key"


def test_projects_create_forwards_caller_key_and_settings(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "projects",
        [
            "create",
            "demo",
            "--name",
            "Demo",
            "--settings",
            '{"owner": "team"}',
            "--idempotency-key",
            "caller-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    (verb, kwargs) = client.calls[0]
    assert verb == "projects.create"
    assert kwargs["metadata"] == {"owner": "team"}
    assert kwargs["idempotency_key"] == "caller-key"
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["idempotency_key"] == "caller-key"


def test_projects_list_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("projects", ["list", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("projects.list", {})]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"] == [{"slug": "demo", "name": "Demo"}]
    assert envelope["receipt"] is None
    assert envelope["idempotency_key"] == ""


def test_projects_show_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("projects", ["show", "demo"], client=client)
    assert rc == 0
    assert client.calls == [("projects.show", {"ref": "demo"})]
    out = capsys.readouterr().out
    assert json.loads(out)["data"]["slug"] == "demo"


def test_projects_update_is_one_sdk_call_with_delta_and_key(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "projects",
        [
            "update",
            "demo",
            "--name",
            "Renamed",
            "--idempotency-key",
            "upd-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    (verb, kwargs) = client.calls[0]
    assert verb == "projects.update"
    assert kwargs == {
        "ref": "demo",
        "name": "Renamed",
        "metadata": None,
        "idempotency_key": "upd-key",
    }
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["idempotency_key"] == "upd-key"


def test_projects_select_is_one_sdk_call_persisting_preference() -> None:
    client = _FakeClient()
    rc = _run("projects", ["select", "demo"], client=client)
    assert rc == 0
    assert client.calls == [("projects.select", {"ref": "demo", "scope": "workspace", "cwd": None})]


def test_projects_select_scope_user_is_forwarded() -> None:
    client = _FakeClient()
    rc = _run("projects", ["select", "demo", "--scope", "user", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("projects.select", {"ref": "demo", "scope": "user", "cwd": None})]


def test_projects_failure_envelope_exits_one(capsys) -> None:
    class _FailingProjects(_RecordingProjects):
        def show(self, ref):
            self._owner.calls.append(("projects.show", {"ref": ref}))
            return DomainResult.failure(
                ErrorObject(code="not_found", message="missing", details={})
            )

    class _FailingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.projects = _FailingProjects(self)

    client = _FailingClient()
    rc = _run("projects", ["show", "nope"], client=client)
    assert rc == 1
    assert len(client.calls) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "not_found"
    assert json.loads(captured.out)["error"]["message"] == "missing"


def test_projects_unknown_verb_is_a_usage_error() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("projects", ["export", "demo"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_projects_malformed_settings_is_a_usage_error() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "projects",
            ["create", "demo", "--name", "Demo", "--settings", "not-json"],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["create", "--help"],
        ["list", "--help"],
        ["show", "--help"],
        ["update", "--help"],
        ["select", "--help"],
        ["current", "--help"],
    ],
)
def test_projects_help_is_executable(argv: list[str]) -> None:
    from astrid.core.cli.domain_projects import build_parser

    parser = build_parser(_FakeClient())
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# T28 — timelines family
# ---------------------------------------------------------------------------


def test_timelines_parser_has_visualize_and_no_aliases() -> None:
    from astrid.packs.timeline.cli import COMMANDS, build_parser

    assert tuple(spec.name for spec in COMMANDS) == (
        "checkout",
        "edit",
        "assets",
        "find",
        "apply",
        "undo",
        "redo",
        "check",
        "status",
        "publish",
        "discard",
        "duplicate",
        "lines",
        "words",
        "create",
        "list",
        "show",
        "script",
        "replace-parent-media",
        "archive",
        "recover",
        "history",
        "log",
        "diff",
        "visualize",
        "lint",
        "inspect",
        "render",
    )
    assert all(spec.aliases == () for spec in COMMANDS)
    # The parser registers the timeline verbs plus the
    # manifest-declared nested ``shots`` mount (task T29).
    assert _subparser_choices(build_parser(_FakeClient())) == {
        "checkout",
        "edit",
        "assets",
        "find",
        "apply",
        "undo",
        "redo",
        "check",
        "status",
        "publish",
        "discard",
        "duplicate",
        "lines",
        "words",
        "create",
        "list",
        "show",
        "script",
        "replace-parent-media",
        "archive",
        "recover",
        "history",
        "log",
        "diff",
        "visualize",
        "lint",
        "inspect",
        "render",
        "shots",
    }


def test_timelines_render_help_includes_copyable_flat_profile(capsys) -> None:
    from astrid.packs.timeline.cli import build_parser

    parser = build_parser(_FakeClient())
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["render", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    normalized = " ".join(help_text.split())
    assert "Flat RenderProfile v1 JSON object (no video/audio nesting)" in normalized
    assert '"fps_rational": [30, 1]' in help_text
    assert '"time_base": [1, 90000]' in help_text
    assert '"audio_channel_layout": "stereo"' in help_text


def test_timelines_render_translates_public_backend_to_executor_selector(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        [
            "render",
            "--project",
            "demo",
            "main",
            "--expected-version",
            "5",
            "--backend",
            "rendering.ffmpeg",
            "--output-name",
            "selected.mp4",
            "--timeout-seconds",
            "90",
            "--json",
        ],
        client=client,
    )

    assert rc == 0
    assert len(client.calls) == 1
    verb, kwargs = client.calls[0]
    assert verb == "invoke_result"
    assert kwargs["capability_id"] == "rendering.render"
    assert kwargs["kind"] == "executor"
    assert kwargs["project"] == "demo"
    assert kwargs["wait"] is True
    assert kwargs["timeout_seconds"] == 90.0
    assert kwargs["inputs"] == {
        "timeline_ref": "main",
        "expected_version": 5,
        "selector": "rendering.ffmpeg",
        "output_name": "selected.mp4",
    }
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_completed_render_prints_its_video_handle(capsys) -> None:
    """Users guessed run:R/output#0 to export the film; the render now names run:R/video#0."""
    client = _FakeClient()
    original = client.invoke_result

    def with_video(capability_id, **kwargs):
        import dataclasses

        result = original(capability_id, **kwargs)
        return dataclasses.replace(result, outputs={
            **dict(result.outputs or {}),
            "managed_outputs": [{"output_port": "video", "ordinal": 0, "filename": "film.mp4"}]})

    client.invoke_result = with_video
    rc = _run("timelines", ["render", "--project", "demo", "main", "--json"], client=client)

    assert rc == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["video"] == "run:R-1/video#0"
    assert data["export"] == "media open run:R-1/video#0 --project demo --to FILE.mp4"


def test_timelines_render_detach_is_explicit_and_truthfully_labeled(capsys) -> None:
    client = _FakeClient()

    rc = _run(
        "timelines",
        ["render", "--project", "demo", "main", "--detach", "--json"],
        client=client,
    )

    assert rc == 0
    _, kwargs = client.calls[0]
    assert kwargs["wait"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["state"] == "admitted"
    assert payload["data"]["handoff"] == {
        "events": "python3 -m astrid tasks events T-1 --project demo --json",
        "follow": "python3 -m astrid tasks follow T-1 --project demo",
        "inspect": "python3 -m astrid tasks show T-1 --project demo --json",
        "open": "python3 -m astrid runs open R-1 --project demo",
        "recent": "python3 -m astrid tasks list --project demo --json",
    }


def test_timelines_render_default_admission_preserves_json_handoff(capsys) -> None:
    client = _FakeClient()

    assert _run(
        "timelines", ["render", "--project", "demo", "main", "--detach", "--json"], client=client
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["state"] == "admitted"
    assert payload["data"]["handoff"]["open"] == "python3 -m astrid runs open R-1 --project demo"



class _DefaultTimelineProjects(_RecordingProjects):
    def show(self, ref):
        self._owner.calls.append(("projects.show", {"ref": ref}))
        return DomainResult.success(
            {
                "slug": ref,
                "name": "Demo",
                "project_id": "P-1",
                "metadata": {"default_timeline_id": "TL-9"},
            }
        )


class _DefaultTimelineClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.projects = _DefaultTimelineProjects(self)


def test_timelines_render_without_ref_delegates_default_resolution(capsys) -> None:
    client = _DefaultTimelineClient()
    rc = _run(
        "timelines",
        ["render", "--project", "demo", "--detach", "--json"],
        client=client,
    )
    assert rc == 0
    _, kwargs = client.calls[0]
    assert kwargs["capability_id"] == "rendering.render"
    assert kwargs["inputs"] == {}
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_timelines_render_without_ref_forwards_to_runtime_admission(capsys) -> None:
    client = _FakeClient()
    rc = _run("timelines", ["render", "--project", "demo", "--json"], client=client)
    assert rc == 0
    assert [verb for verb, _ in client.calls] == ["invoke_result"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_timelines_render_without_ref_surfaces_typed_runtime_error(capsys) -> None:
    class _FailingClient(_FakeClient):
        def invoke_result(self, capability_id, **kwargs):
            self.calls.append(("invoke_result", {"capability_id": capability_id, **kwargs}))
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=False,
                error={
                    "message": "project has no configured default canonical timeline",
                    "sdk_category": "precondition",
                },
                raw_result={"ok": False},
            )

    client = _FailingClient()
    rc = _run("timelines", ["render", "--json"], client=client)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "default canonical timeline" in payload["error"]["message"]


def test_timelines_visualize_help_describes_filmstrip_navigation(
    capsys,
) -> None:
    from astrid.packs.timeline.cli import build_parser

    parser = build_parser(_FakeClient())
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["visualize", "--help"])
    assert exc_info.value.code == 0
    normalized = " ".join(capsys.readouterr().out.split())
    assert "Timeline slug, UUID, or ULID" in normalized
    assert "Rendered paired filmstrip" in normalized
    assert "structure" not in normalized
    assert "raw frames (frames/, frame-index.json) to DIR" in normalized
    assert "--include-media" in normalized
    assert "timeline_editing" in normalized
    assert "checkout" in normalized


@pytest.mark.parametrize(
    "forbidden",
    [
        "copy",
        "migrate",
        "migration",
        "push",
        "pull",
        "sync",
        "audit",
        "erase",
        "repair",
        "ls",
    ],
)
def test_timelines_forbidden_and_legacy_verbs_are_absent(forbidden: str) -> None:
    from astrid.packs.timeline.cli import build_parser

    parser = build_parser(_FakeClient())
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([forbidden])
    assert excinfo.value.code == 2






def test_timelines_list_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("timelines", ["list", "--project", "demo", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("timelines.list", {"project": "demo"})]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"] == [{"slug": "main", "name": "Main"}]


def test_timelines_list_compacts_document_blobs_to_identity_and_counts(capsys) -> None:
    class _LargeListing(_RecordingTimelines):
        def list(self, project, *, include_archived=False):
            self._owner.calls.append(("timelines.list", {"project": project}))
            return DomainResult.success(
                [
                    {
                        "timeline_id": "T-1",
                        "slug": "main",
                        "name": "Main",
                        "version": 7,
                        "is_default": True,
                        "config": {
                            "fps": 24,
                            "clips": [{"id": "C-1"}, {"id": "C-2"}],
                            "opaque": "large config omitted",
                        },
                        "registry": {"assets": {"A-1": {}, "A-2": {}}},
                    }
                ]
            )

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__()
            self.timelines = _LargeListing(self)

    client = _Client()
    assert _run("timelines", ["list", "--project", "demo", "--json"], client=client) == 0
    data = json.loads(capsys.readouterr().out)["data"][0]
    assert data == {
        "timeline_id": "T-1",
        "slug": "main",
        "name": "Main",
        "version": 7,
        "is_default": True,
        "counts": {"config_keys": 3, "clips": 2, "assets": 2},
    }


def test_timelines_list_forwards_include_archived(capsys) -> None:
    client = _FakeClient()
    assert _run(
        "timelines", ["list", "--project", "demo", "--include-archived", "--json"], client=client
    ) == 0
    assert client.calls == [("timelines.list", {"project": "demo", "include_archived": True})]
    capsys.readouterr()


def test_timelines_list_compacts_runtime_page_and_preserves_cursor(capsys) -> None:
    class _PagedListing(_RecordingTimelines):
        def list(self, project, *, include_archived=False):
            self._owner.calls.append(("timelines.list", {"project": project}))
            return DomainResult.success(
                [
                    [{"timeline_id": "T-1", "slug": "main", "config": {"clips": [1]}}],
                    "next-page",
                ]
            )

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__()
            self.timelines = _PagedListing(self)

    client = _Client()
    assert _run("timelines", ["list", "--project", "demo", "--json"], client=client) == 0
    assert json.loads(capsys.readouterr().out)["data"] == [
        [{"timeline_id": "T-1", "slug": "main", "counts": {"config_keys": 1, "clips": 1}}],
        "next-page",
    ]


def test_remote_timelines_list_filters_archived_rows_and_preserves_cursor() -> None:
    class _Runtime:
        def list_timelines(self, project, *, cursor=None, limit=50):
            assert project == "demo"
            return (
                [
                    {"timeline_id": "T-archived", "archived": True},
                    {"timeline_id": "T-active", "archived": False},
                ],
                "next-page",
            )

    service = RemoteTimelines(_Runtime())
    result = service.list("demo")
    assert result.ok is True
    assert result.data == [[{"timeline_id": "T-active", "archived": False}], "next-page"]
    inclusive = service.list("demo", include_archived=True)
    assert inclusive.data[0] == [
        {"timeline_id": "T-archived", "archived": True},
        {"timeline_id": "T-active", "archived": False},
    ]


def test_timelines_show_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("timelines", ["show", "--project", "demo", "main", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("timelines.open_composition", {
        "project": "demo", "ref": "main", "limit": 50, "cursor": None,
        "clip": None, "occurrence": None, "shot": None, "track": [],
        "asset": None, "range_value": None, "detail": False, "revision_id": None,
    })]
    assert json.loads(capsys.readouterr().out)["data"]["kind"] == "timeline-inspection"


def test_timelines_show_defaults_to_readable_authored_rows(capsys) -> None:
    from astrid.packs.rendering.actions.timeline_visualize.readable_timing import project_readable_timing
    raw = {"id": "clip-a", "clipType": "media", "track": "picture", "at": 20.5,
           "hold": 16 / 15, "effects": {"fade_in": .5},
           "params": {"sourceSegments": [{"at": 0, "sourceStart": 0, "speed": 1}]}}
    clip = {"occurrence_id": "occ-a", "clip_id": "clip-a", "track": "picture", "clip_type": "media",
            "start": [41, 2], "duration": [16, 15], "asset": "terminal.png",
            "authored_fields": raw, "render_timing": raw}
    context = {"status": "complete", "fps": 30, "tracks": [{"id": "picture", "kind": "visual"}], "clips": [raw]}

    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "head-1"},
                "scope": {"timeline": ref}, "query": {},
                "clips": project_readable_timing([clip], context), "pagination": {},
                "navigation": {"commands": {"visualize": "astrid timelines visualize --occurrence occ-a"}},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert "Authored timeline" in output
    assert "20.5–21.566667" in output
    assert "opacity multiplier 0% → 100% (fade in)" in output
    assert "source playback segments use a separate media clock" in output
    assert "visualize: python3 -m astrid timelines visualize" in output
    assert "controls: python3 -m astrid timelines visualize --help" in output
    assert "edit guide:" in output
    assert "edit JSON (current head): python3 -m astrid.packs.rendering.docs.scripts.timeline_document checkout" in output


def test_timeline_navigation_preserves_resolved_scope_and_editing_resources() -> None:
    from astrid.packs.timeline.cli import _show_navigation_help, _visualization_navigation_help

    parsed = SimpleNamespace(
        revision_id=None,
        occurrence=None,
        shot=None,
        clip=None,
        asset=None,
        range=None,
        track=None,
    )
    outputs = {
        "summary": {
            "project_slug": "astrid-intro",
            "timeline_slug": "main-final",
            "revision_id": "rev-42",
        }
    }
    shown = _show_navigation_help(project=None, ref=None, parsed=parsed, outputs=outputs)
    assert "--project astrid-intro" in shown["commands"]["visualize"]
    assert "--timeline-slug main-final" in shown["commands"]["visualize"]
    assert "--revision-id rev-42" in shown["commands"]["visualize"]
    assert shown["editing"]["workflow"] == ["checkout", "edit", "check", "publish"]
    assert shown["editing"]["checkout_revision"].startswith("current Runtime head")
    assert Path(shown["editing"]["guide"]).is_file()
    assert Path(shown["editing"]["script"]).is_file()

    visual = _visualization_navigation_help(
        project=None,
        inputs={"view": "filmstrip", "timeline_slug": None, "show": ["output"]},
        outputs={**outputs, "static_surface": {"components": ["output"]}},
    )
    assert "--project astrid-intro" in visual["commands"]["show"]
    assert "main-final" in visual["commands"]["show"]
    assert "--revision-id rev-42" in visual["commands"]["show"]
    assert "--frame FRAME" in visual["commands"]["exact_frame"]
    assert visual["editing"]["commands"]["check"].endswith("--file /tmp/timeline-edit.json")


def test_visualization_human_navigation_footer_is_compact(capsys) -> None:
    from astrid.packs.timeline.cli import _print_visualization_navigation

    _print_visualization_navigation(
        {
            "navigation": {
                "primary_page": "/tmp/page-001.png",
                "inspection": "python3 -m astrid timelines inspect --manifest MANIFEST",
                "commands": {
                    "visualize": "python3 -m astrid timelines visualize --project demo",
                    "show": "python3 -m astrid timelines show --project demo main",
                    "controls": "python3 -m astrid timelines visualize --help",
                    "exact_frame": "python3 -m astrid timelines visualize --frame FRAME",
                },
                "editing": {
                    "guide": "/tmp/document-checkout.md",
                    "commands": {
                        "checkout": "python3 -m astrid.packs.rendering.docs.scripts.timeline_document checkout",
                    },
                },
            }
        }
    )
    output = capsys.readouterr().out
    assert "show: python3 -m astrid timelines show" in output
    assert "controls: python3 -m astrid timelines visualize --help" in output
    assert "edit JSON (current head):" in output
    assert "visualize: python3 -m astrid timelines visualize" not in output
    assert "exact frame:" not in output


def test_timelines_show_reads_runtime_motion_and_scoped_friendly_media(capsys, tmp_path) -> None:
    """Exercise actual Runtime closure -> SDK projection -> human CLI output."""
    runtime_root = os.environ.get("ASTRID_RUNTIME_SOURCE_ROOT")
    if not runtime_root:
        pytest.skip("set ASTRID_RUNTIME_SOURCE_ROOT to the paired Runtime execution worktree")
    runtime_path = Path(runtime_root).resolve()
    if not (runtime_path / "runtime_protocol" / "service.py").is_file():
        pytest.fail(f"ASTRID_RUNTIME_SOURCE_ROOT has no runtime_protocol package: {runtime_path}")
    sys.path.insert(0, str(runtime_path))
    try:
        from runtime_protocol.service import RuntimeService
        from runtime_protocol.store import RealmStore

        root = tmp_path / "runtime-realm"
        RealmStore.initialize(root, display_name="Timeline inspection fixture").close()
        service = RuntimeService(root)
        try:
            project = service.create_project({"slug": "inspection", "name": "Inspection"}, idempotency_key="project")
            project_id = project["id"]
            service.create_timeline(project_id, "main", idempotency_key="timeline")

            def ingest_media(name: str, data: bytes) -> dict[str, str]:
                row = service.ingest(
                    project_id, data, media_type="image/png", original_name=name,
                    idempotency_key=f"media-{name}",
                )["data"]
                return {"object_id": row["object_id"], "digest": row["digest"]}

            room = ingest_media("shot-room-final.png", b"room-image")
            studio = ingest_media("shot-studio-final.png", b"studio-image")
            parent_media = ingest_media("global-banner.png", b"parent-image")

            def internal_payload(revision: str, media: dict[str, str], end_width: int, *, moving: bool) -> dict[str, Any]:
                clip = {
                    "id": "room-clip",
                    "clipType": "animated-media-transform",
                    "track": "outer-picture",
                    "at": 0,
                    "hold": 16 / 15 if revision in {"internal-room-before", "internal-room-after"} else 1.1,
                    "asset": "black_frame",
                }
                if moving:
                    clip["params"] = {"keyframes": [
                        {"at": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "opacity": 1},
                        {"at": clip["hold"], "x": 0, "y": 0, "width": end_width, "height": 1080, "opacity": 1},
                    ]}
                return {
                    "timeline_id": "main", "revision_id": revision,
                    "payload": {
                        "tracks": [{"id": "outer-picture", "kind": "visual"}],
                        "clips": [clip], "effects": [], "audio": [], "layout": {},
                        "registry": {"assets": {"black_frame": {
                            "media_id": media["object_id"], "content_sha256": media["digest"],
                            "type": "image", "file": f"assets/{'shot-room-final.png' if media is room else 'shot-studio-final.png'}",
                        }}},
                        "assets": [],
                    },
                }

            def shot_revision(shot_id: str, revision: str, internal_id: str) -> dict[str, Any]:
                return {
                    "shot_id": shot_id, "revision_id": revision,
                    "internal_timeline_revision_id": internal_id,
                    "payload": {"metadata": {"title": shot_id}, "items": [], "pools": [],
                                "selected_variants": {}, "provenance": {}, "generation_inputs": {},
                                "audio_bindings": [], "text_bindings": []},
                }

            def occurrence(oid: str, shot_id: str, shot_rev: str, start_ms: int, duration_ms: int) -> dict[str, Any]:
                return {
                    "occurrence_id": oid, "shot_id": shot_id, "shot_revision_id": shot_rev,
                    "placement": {"start_ms": start_ms}, "duration_ms": duration_ms,
                    "source_offset": 0, "speed": 1, "track": "outer-picture",
                    "transform": {}, "gain": 1, "mute": False, "provenance": {},
                }

            common_parent = {
                "config": {"output": {"fps": 30}, "tracks": [{"id": "outer-picture", "kind": "visual"}]},
                "registry": {"assets": {"black_frame": {
                    "media_id": parent_media["object_id"], "content_sha256": parent_media["digest"],
                    "type": "image", "file": "assets/global-banner.png",
                }}},
                "clips": [{"id": "parent-banner", "clipType": "media", "track": "outer-picture",
                           "at_ms": 0, "duration_ms": 1000, "asset": "black_frame"}],
                "occurrences": [
                    occurrence("occ-room", "shot-room", "shot-room-r1", 20500, 16000),
                    occurrence("occ-studio", "shot-studio", "shot-studio-r1", 33800, 1100),
                ],
            }

            def publication(revision: str, expected_head: str | None, *, room_internal: str, room_shot: str, room_moving: bool) -> dict[str, Any]:
                room_clip = internal_payload(room_internal, room, 4243, moving=room_moving)
                studio_clip = internal_payload("internal-studio", studio, 3840, moving=True)
                parent = json.loads(json.dumps(common_parent))
                parent["occurrences"][0]["shot_revision_id"] = room_shot
                return {
                    "project_id": project_id, "timeline_id": "main", "expected_head": expected_head,
                    "parent_revision_id": revision,
                    "internal_timeline_revisions": [room_clip, studio_clip],
                    "shot_revisions": [
                        shot_revision("shot-room", room_shot, room_internal),
                        shot_revision("shot-studio", "shot-studio-r1", "internal-studio"),
                    ],
                    "parent_composition": parent,
                    "dependency_manifest": {"media": [
                        {"media_id": value["digest"], "content_digest": value["digest"]}
                        for value in (room, studio, parent_media)
                    ]},
                }

            service.publish_parent_composition(
                project_id, "main", publication("parent-before", None,
                                                  room_internal="internal-room-before",
                                                  room_shot="shot-room-r1", room_moving=True),
                idempotency_key="publish-before",
            )

            def list_timelines(project_ref: str, *, cursor=None, limit=50):
                page = service.list_timelines(project_ref, cursor=cursor, limit=limit)
                return page["items"], page["next_cursor"]

            transport = SimpleNamespace(
                list_timelines=list_timelines,
                inspect_timeline=service.inspect_timeline,
            )
            sdk = RemoteTimelines(transport)
            client = SimpleNamespace(timelines=sdk)

            def show(revision: str) -> str:
                assert _run("timelines", ["show", "--project", project_id, "main",
                                           "--revision-id", revision, "--detail"], client=client) == 0
                return capsys.readouterr().out

            before = show("parent-before")
            assert "20.5–21.566667" in before and "33.8–34.9" in before
            assert "shot-room-final.png" in before and "shot-studio-final.png" in before
            assert "global-banner.png" in before
            assert "black_frame" in before
            assert '"source_object_id":"' + room["object_id"] + '"' in before
            assert "width 1920 → 4243" in before and "width 1920 → 3840" in before

            service.publish_parent_composition(
                project_id, "main", publication("parent-after", "parent-before",
                                                  room_internal="internal-room-after",
                                                  room_shot="shot-room-r2", room_moving=False),
                idempotency_key="publish-after",
            )
            after = show("parent-after")
            assert "20.5–21.566667" in after and "33.8–34.9" in after
            assert "shot-room-final.png" in after and "shot-studio-final.png" in after
            assert "width 1920 → 4243" not in after
            assert "width 1920 → 3840" in after
            assert '"source_object_id":"' + room["object_id"] + '"' in after
            assert "saved revision (not current head)" not in after
        finally:
            service.close()
    finally:
        sys.path.remove(str(runtime_path))


def test_timelines_show_human_output_reads_transform_keyframes(capsys) -> None:
    from astrid.packs.rendering.actions.timeline_visualize.readable_timing import project_readable_timing
    raw = {"id": "transform", "clipType": "animated-media-transform", "track": "picture", "at": 10, "hold": 2,
           "params": {"keyframes": [
               {"at": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "opacity": 1},
               {"at": 1, "x": 160, "y": 40, "width": 1280, "height": 720, "opacity": .8}]}}
    clip = {"occurrence_id": "occ-transform", "clip_id": "transform", "start": [10, 1], "duration": [2, 1],
            "clip_type": "animated-media-transform", "asset": "screen.png", "authored_fields": raw, "render_timing": raw}
    context = {"status": "complete", "fps": 30, "tracks": [{"id": "picture", "kind": "visual"}], "clips": [raw]}

    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "head-transform"},
                "scope": {"timeline": ref}, "query": {}, "clips": project_readable_timing([clip], context), "pagination": {}})

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert "10–11 transform" in output
    assert "x 0 → 160" in output
    assert "opacity 1 → 0.8" in output


def test_timelines_show_human_output_keeps_audio_detail_and_unknowns(capsys) -> None:
    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "head-2"},
                "scope": {"timeline": ref}, "query": {"detail": True},
                "clips": [
                    {"occurrence_id": "occ-v", "clip_id": "visual", "track_id": "V1",
                     "track": {"id": "V1", "opacity": 0.3, "blendMode": "screen"},
                     "start": [0, 1], "duration": [2, 1], "asset": "room.png",
                     "transition": {"id": "cross-fade"},
                     "authored_fields": {"label": "room", "params": {"keyframes": []}},
                     "source_object_id": "obj-1", "content_digest": "sha256:abc"},
                    {"occurrence_id": "occ-a", "clip_id": "audio", "clip_type": "audio",
                     "track": "A1", "start": [0, 1], "duration": [2, 1], "asset": "music.wav", "mute": True},
                    {"occurrence_id": "occ-u", "clip_id": "opaque", "start": [2, 1], "duration": [1, 1],
                     "asset": "opaque", "timing_unknown": True},
                ],
                "pagination": {},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main", "--detail"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert "Track V1 [authored track controls: opacity 30%, blend screen]" in output
    assert "Audio" in output and "0–2  music.wav · occ-a/audio [muted]" in output
    assert "[transition cross-fade; timing unresolved: producer unavailable]" in output
    assert 'authored label: "room"' in output
    assert 'params: {"keyframes":[]}' in output
    assert "identity:" in output and "obj-1" in output
    assert "motion timing unknown: authored clock explicitly unknown" in output


def test_timelines_show_groups_exact_scoped_tracks_and_keeps_addressable_audio(capsys) -> None:
    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection",
                "summary": {"revision_id": "saved-r9", "is_current_head": False},
                "scope": {"timeline": ref},
                "query": {"detail": True},
                "clips": [
                    {
                        "occurrence_id": "occ-a-late", "clip_id": "visual-late",
                        "track_id": "shared", "track_ref": {"scope": "internal_timeline", "scope_id": "child-a", "track_id": "shared"},
                        "track": {"id": "shared", "kind": "visual", "opacity": 0.3, "blendMode": "screen", "volume": 0},
                        "start": [3, 1], "duration": [1, 1], "media_name": "room.png",
                        "asset_id": "black_frame", "source_object_id": "obj-room", "content_digest": "sha256:room",
                        "authored_fields": {"label": "towards monitor", "params": {"tint": "cool"}},
                    },
                    {
                        "occurrence_id": "occ-a-early", "clip_id": "visual-early",
                        "track_id": "shared", "track_ref": {"scope": "internal_timeline", "scope_id": "child-a", "track_id": "shared"},
                        "track": {"id": "shared", "kind": "visual", "opacity": 0.3, "blendMode": "screen", "volume": 0},
                        "start": [1, 1], "duration": [1, 1], "media_name": "room.png",
                        "asset_id": "black_frame", "source_object_id": "obj-room", "content_digest": "sha256:room",
                    },
                    {
                        "occurrence_id": "occ-b", "clip_id": "visual-b",
                        "track_id": "shared", "track_ref": {"scope": "internal_timeline", "scope_id": "child-b", "track_id": "shared"},
                        "track": {"id": "shared", "kind": "visual", "mute": True},
                        "start": [2, 1], "duration": [1, 1], "media_name": "studio.png",
                        "asset_id": "black_frame", "source_object_id": "obj-studio", "content_digest": "sha256:studio",
                    },
                    {
                        "occurrence_id": "occ-audio", "clip_id": "audio-1", "kind": None,
                        "clip_type": "audio", "track_id": "shared",
                        "track_ref": {"scope": "internal_timeline", "scope_id": "audio-child", "track_id": "shared"},
                        "track": {"id": "shared", "kind": "audio"},
                        "start": [5, 1], "duration": [2, 1], "media_name": "theme.wav",
                        "asset_id": "music_alias", "source_object_id": "obj-music", "content_digest": "sha256:music",
                        "gain": 0, "mute": True, "parameters": {"fadeIn": 0.5}, "parameters_source": "params",
                    },
                ],
                "native_inspection": {"selected": [
                    {"occurrence": {"occurrence_id": "occ-a-late", "shot_id": "shot-a", "track_ref": {"scope": "parent_composition", "scope_id": "parent-1", "track_id": "outer"}, "track": {"id": "outer", "opacity": 0.5}, "mute": True}},
                    {"occurrence": {"occurrence_id": "occ-a-early", "shot_id": "shot-a", "track_ref": {"scope": "parent_composition", "scope_id": "parent-1", "track_id": "outer"}, "track": {"id": "outer", "opacity": 0.5}, "mute": True}},
                    {"occurrence": {"occurrence_id": "occ-b", "shot_id": "shot-b", "track_ref": {"scope": "parent_composition", "scope_id": "parent-2", "track_id": "outer"}, "track": {"id": "outer", "mute": True}}},
                    {"occurrence": {"occurrence_id": "occ-audio", "shot_id": "shot-audio", "track_ref": {"scope": "parent_composition", "scope_id": "parent-3", "track_id": "outer"}, "track": {"id": "outer", "volume": 0}}},
                ]},
                "page": {"returned_clips": 4, "total_selected_clips": 4, "remaining_clips": 0},
                "pagination": {},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main", "--detail"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert "Visual layers (order unavailable; grouped by exact scoped track)" in output
    assert "Track shared (internal_timeline/child-a) [authored track controls: opacity 30%, blend screen, gain 0]" in output
    assert "Track shared (internal_timeline/child-b) [authored track controls: muted]" in output
    assert output.index("1. 1–2  room.png · occ-a-early/visual-early") < output.index("2. 3–4  room.png · occ-a-late/visual-late")
    assert "occurrences occ-a-late, occ-a-early [opacity 50%, muted]" in output
    assert "Audio (separate tracks; order unavailable unless projected)" in output
    assert "Track shared (internal_timeline/audio-child)" in output
    assert "5–7  theme.wav · occ-audio/audio-1 [muted, gain 0]" in output
    assert 'authored label: "towards monitor"' in output
    assert 'params: {"tint":"cool"}' in output
    assert '"asset_id":"black_frame"' in output
    assert '"source_object_id":"obj-room"' in output
    assert "saved revision (not current head)" in output


@pytest.mark.parametrize(
    ("clip", "expect_audio"),
    [
        (
            {
                "occurrence_id": "occ-visual-audio",
                "clip_id": "child-visual",
                "clip_type": "media",
                "track_ref": {"scope": "internal_timeline", "scope_id": "child-a", "track_id": "shared"},
                "track": {"id": "shared", "kind": "visual", "opacity": 0.5, "blendMode": "multiply"},
                "start": [0, 1], "duration": [2, 1], "media_name": "room.png",
                "compositor_dispatch": {
                    "status": "resolved", "fade_contract": "audio_params", "control_contract": "audio_track",
                    "track": {"id": "outer-audio", "kind": "audio", "muted": True, "volume": 0.4},
                    "controls": {"base_gain": 0},
                },
                "timed_changes": [{"kind": "fade_in", "property": "gain multiplier", "start": [0, 1],
                                   "end": [1, 2], "before": 0, "after": 1}],
            },
            True,
        ),
        (
            {
                "occurrence_id": "occ-audio-visual",
                "clip_id": "child-audio",
                "clip_type": "audio",
                "track_ref": {"scope": "internal_timeline", "scope_id": "child-b", "track_id": "shared"},
                "track": {"id": "shared", "kind": "audio", "muted": True, "volume": 0.1},
                "volume": 0.5,
                "start": [0, 1], "duration": [2, 1], "media_name": "room.wav",
                "compositor_dispatch": {
                    "status": "resolved", "fade_contract": "visual_effects", "control_contract": "visual_media",
                    "track": {"id": "outer-visual", "kind": "visual", "opacity": 0.3, "blendMode": "screen"},
                    "controls": {"base_gain": 0.2, "track_opacity_multiplier": 0.3,
                                 "clip_opacity_multiplier": 0.6},
                },
                "timed_changes": [{"kind": "fade_in", "property": "opacity multiplier", "start": [0, 1],
                                   "end": [1, 2], "before": 0, "after": 1}],
            },
            False,
        ),
    ],
)
def test_timelines_show_uses_compositor_dispatch_for_human_classification_and_controls(
    capsys, clip, expect_audio
) -> None:
    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "dispatch-r1"},
                "scope": {"timeline": ref}, "query": {}, "clips": [clip], "pagination": {},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    audio_section = "Audio (separate tracks; order unavailable unless projected)"
    assert (audio_section in output) is expect_audio
    if clip["clip_id"] == "child-visual":
        assert output.index(audio_section) < output.index("Track shared (internal_timeline/child-a)")
        assert "Track shared (internal_timeline/child-a) [authored track controls: opacity 50%, blend multiply]" in output
        assert "compositor dispatch: audio track outer-audio [muted, parent volume 0.4, effective base gain 0]" in output
        assert "gain multiplier 0% → 100%" in output
    else:
        assert "Track shared (internal_timeline/child-b) [authored track controls: muted, gain 0.1]" in output
        assert "room.wav · occ-audio-visual/child-audio [authored gain 0.5]" in output
        assert "compositor dispatch: visual track outer-visual [effective track opacity 30%, blend screen]" in output
        assert "compositor dispatch clip controls: effective clip opacity 60%" in output
        assert "opacity multiplier 0% → 100%" in output
        assert "gain multiplier" not in output


def test_timelines_show_places_shared_muted_dispatch_once_on_exact_track_heading(capsys) -> None:
    clips = [
        {
            "occurrence_id": f"occ-{suffix}", "clip_id": f"clip-{suffix}", "clip_type": "media",
            "track_ref": {"scope": "internal_timeline", "scope_id": "child-audio", "track_id": "shared"},
            "track": {"id": "shared", "kind": "visual", "opacity": 0.3, "blendMode": "screen"},
            "start": [index * 2, 1], "duration": [1, 1], "media_name": f"room-{suffix}.wav",
            "compositor_dispatch": {
                "status": "resolved", "control_contract": "audio_track",
                "track": {"id": "outer-audio", "kind": "audio", "muted": True, "volume": 0.4},
                "controls": {"base_gain": 0},
            },
        }
        for index, suffix in enumerate(("a", "b"))
    ]

    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "dispatch-shared-r1"},
                "scope": {"timeline": ref}, "query": {}, "clips": clips, "pagination": {},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    dispatch = "compositor dispatch: audio track outer-audio [muted, parent volume 0.4, effective base gain 0]"
    heading = "Track shared (internal_timeline/child-audio) [authored track controls: opacity 30%, blend screen]"
    assert output.count(dispatch) == 1
    assert f"{heading} [{dispatch}]" in output
    assert output.index(f"{heading} [{dispatch}]") < output.index("room-a.wav · occ-a/clip-a")
    assert "room-b.wav · occ-b/clip-b" in output


def test_timelines_show_clip_specific_effective_gain_on_each_row(capsys) -> None:
    clips = [
        {
            "occurrence_id": f"occ-{suffix}", "clip_id": f"clip-{suffix}", "clip_type": "audio",
            "track_ref": {"scope": "internal_timeline", "scope_id": "child-audio", "track_id": "shared"},
            "track": {"id": "shared", "kind": "audio"}, "volume": volume,
            "start": [index * 2, 1], "duration": [1, 1], "media_name": f"sound-{suffix}.wav",
            "compositor_dispatch": {
                "status": "resolved", "control_contract": "audio_track",
                "track": {"id": "outer-audio", "kind": "audio", "volume": 0.4},
                "controls": {"base_gain": base_gain},
            },
        }
        for index, (suffix, volume, base_gain) in enumerate((("a", 0.5, 0.2), ("b", 0.7, 0.28)))
    ]

    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "dispatch-clip-gains-r1"},
                "scope": {"timeline": ref}, "query": {}, "clips": clips, "pagination": {},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert output.count("compositor dispatch: audio track outer-audio [parent volume 0.4]") == 1
    assert "compositor dispatch clip controls: effective base gain 0.2" in output
    assert "compositor dispatch clip controls: effective base gain 0.28" in output
    assert "sound-a.wav · occ-a/clip-a [authored gain 0.5]" in output
    assert "sound-b.wav · occ-b/clip-b [authored gain 0.7]" in output


def test_timelines_show_names_unresolved_compositor_dispatch(capsys) -> None:
    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"revision_id": "dispatch-unknown"},
                "scope": {"timeline": ref}, "query": {}, "clips": [{
                    "occurrence_id": "occ-unknown", "clip_id": "clip-unknown", "clip_type": "audio",
                    "track": {"id": "child-audio", "kind": "audio"},
                    "track_ref": {"scope": "internal_timeline", "scope_id": "child", "track_id": "child-audio"},
                    "start": [0, 1], "duration": [1, 1], "media_name": "unknown.wav",
                    "compositor_dispatch": {"status": "unknown", "reason": "dispatch track ambiguous"},
                    "timed_changes": [],
                }], "pagination": {},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert "Audio (separate tracks; order unavailable unless projected)" in output
    assert "[compositor dispatch unknown: dispatch track ambiguous]" in output


def test_timelines_show_omission_notice_names_exact_pinned_target_and_limit(capsys) -> None:
    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection",
                "summary": {"revision_id": "rev-omitted", "is_current_head": True},
                "scope": {"timeline": ref}, "query": {"detail": True},
                "clips": [{
                    "occurrence_id": "occ-omitted", "shot_id": "shot-omitted", "clip_id": "clip-omitted",
                    "media_name": "room.png", "asset_id": "black_frame", "source_object_id": "obj-room",
                    "content_digest": "sha256:room", "start": [0, 1], "duration": [1, 1],
                    "track_ref": {"scope": "internal_timeline", "scope_id": "internal-omitted", "track_id": "picture"},
                    "authored_fields": None,
                    "omitted_fields": [{"path": "parameters", "reason": "byte_limit", "byte_length": 6500,
                                        "limit_bytes": 4096, "sha256": "params-digest"}],
                    "track_omitted_fields": [{"path": "track", "reason": "byte_limit", "byte_length": 6500,
                                               "limit_bytes": 2048, "sha256": "track-digest"}],
                }],
                "omission_metadata": {"authored_values_omitted": 2},
                "page": {"returned_clips": 1, "total_selected_clips": 3, "remaining_clips": 2},
                "pagination": {"next_cursor": "cursor-next"},
            })

    class _Client:
        timelines = _Timelines()

    assert _run("timelines", ["show", "--project", "demo", "main"], client=_Client()) == 0
    output = capsys.readouterr().out
    assert "current head" in output
    assert "returned 1 of 3 selected clips; 2 remain after this page" in output
    assert "omitted parameters: byte_limit (6500 B; limit 4096 B); sha256 params-digest" in output
    assert 'omitted track: byte_limit (6500 B; limit 2048 B); sha256 track-digest' in output
    assert '"revision_id":"rev-omitted"' in output
    assert '"occurrence_id":"occ-omitted"' in output
    assert '"clip_id":"clip-omitted"' in output
    assert '"track_ref":{"scope":"internal_timeline","scope_id":"internal-omitted","track_id":"picture"}' in output
    assert "page: 2 bounded values omitted" in output
    assert "full omitted values are unavailable through a bounded retrieval route" in output

    assert _run("timelines", ["show", "--project", "demo", "main", "--detail"], client=_Client()) == 0
    detail_output = capsys.readouterr().out
    assert "omitted track: byte_limit (6500 B; limit 2048 B); sha256 track-digest" in detail_output
    assert '"track_ref":{"scope":"internal_timeline","scope_id":"internal-omitted","track_id":"picture"}' in detail_output


def test_timelines_show_allows_runtime_selected_scope(capsys) -> None:
    client = _FakeClient()
    rc = _run("timelines", ["show", "--json"], client=client)
    assert rc == 0
    assert client.calls[0][0] == "timelines.open_composition"
    assert client.calls[0][1]["project"] is None
    assert client.calls[0][1]["ref"] is None
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_timelines_show_summary_is_bounded_and_keeps_clip_timing(capsys) -> None:
    class _Timelines:
        def open_composition(self, project, ref, **kwargs):
            return DomainResult.success({
                "kind": "timeline-inspection",
                "summary": {"authority": "canonical_head", "revision_id": "head-1", "is_current_head": True},
                "query": {}, "targets": [],
                "clips": [{"clip_id": "image-1", "track": "picture", "at": 1.0, "duration": 6.0}],
            })

    class _Client:
        timelines = _Timelines()

    rc = _run("timelines", ["show", "--summary", "--project", "demo", "main", "--json"], client=_Client())
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)["data"]
    assert summary["kind"] == "timeline-inspection"
    assert summary["summary"]["authority"] == "canonical_head"
    assert summary["clips"][0]["clip_id"] == "image-1"










def test_timelines_archive_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        ["archive", "--project", "demo", "main", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "timelines.archive",
            {"project": "demo", "ref": "main", "idempotency_key": None},
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["archived"] is True


def test_timelines_list_and_recover_are_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        ["list", "--project", "demo", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [("timelines.list", {"project": "demo"})]
    capsys.readouterr()

    client.calls.clear()
    rc = _run(
        "timelines",
        ["recover", "--project", "demo", "main", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "timelines.recover",
            {"project": "demo", "ref": "main", "idempotency_key": None},
        )
    ]
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "active"


def test_timelines_history_is_one_sdk_call(capsys) -> None:
    # History shows the head revision from the Runtime projection. It never reads the legacy
    # table (timelines.history), and says the earlier revisions cannot be listed (exit 0).
    client = _FakeClient()

    def open_composition(project, ref, *, revision_id=None, limit=50, **kwargs):
        client.calls.append(("timelines.open_composition", {"project": project, "ref": ref, "revision_id": revision_id}))
        head = "authoring-parent-revision-aaaa1111"
        return DomainResult.success({
            "summary": {"head_revision_id": head, "revision_id": revision_id or head, "created_at": "2026-10-01T09:00:00Z"},
            "native_inspection": {},
        })

    client.timelines.open_composition = open_composition
    rc = _run("timelines", ["history", "--project", "demo", "main", "--json"], client=client)
    assert rc == 0
    assert {name for name, _ in client.calls} == {"timelines.open_composition"}
    data = json.loads(capsys.readouterr().out)["data"]
    assert [r["id"] for r in data["revisions"]] == ["authoring-parent-revision-aaaa1111"]
    assert data["complete"] is False
    assert data["reason"].startswith("earlier revisions: this Runtime cannot list them")


def test_timelines_diff_is_one_sdk_call(capsys) -> None:
    # Lifecycle-version diffs stay one SDK call; revision diffs (--from/--to)
    # are covered in tests/sdk/test_timeline_cuts.py.
    client = _FakeClient()
    rc = _run("timelines", ["diff", "--project", "demo", "main", "--from-version", "1", "--to-version", "2", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("timelines.diff", {"project": "demo", "ref": "main", "from_version": 1, "to_version": 2})]
    assert len(json.loads(capsys.readouterr().out)["data"]) == 1


def test_timelines_visualize_routes_public_sdk_and_normalizes_formats(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        [
            "visualize",
            "--project", "demo",
            "--timeline-slug", "01TIMELINE",
            "--format", "png,md",
            "--format", "md",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert len(client.calls) == 1
    verb, kwargs = client.calls[0]
    assert verb == "invoke_result"
    assert kwargs["capability_id"] == "rendering.timeline_visualize"
    assert kwargs["kind"] == "executor"
    assert kwargs["project"] == "demo"
    assert kwargs["wait"] is True
    # No window asked for: the overview (one tile per cut) at review size.
    assert kwargs["inputs"] == {
        "formats": ["png", "md", "md"],
        "timeline_slug": "01TIMELINE",
        "view": "contact",
        "resolution": "480x270",
    }
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"]["run_id"] == "01RUN"
    assert envelope["data"]["outputs"]["artifacts"] == ["/tmp/manifest.json"]
    assert envelope["data"]["outputs"]["artifact_summary"] == {
        "artifact_count": 1,
        "unique_media_count": 0,
        "unique_content_hash_count": 0,
        "duplicate_reference_count": 0,
        "duplicate_group_count": 0,
        "duplicate_groups": [],
    }


def test_visualization_artifact_summary_groups_deduplicated_filmstrip_refs() -> None:
    from astrid.packs.timeline.cli import _visualization_artifact_summary

    summary = _visualization_artifact_summary(
        {
            "artifacts": [
                {"label": "PG001_film_00.png", "media_id": "M1", "content_hash": "H1"},
                {"label": "PG001_film_01.png", "media_id": "M1", "content_hash": "H1"},
                {"label": "manifest.json", "media_id": "M2", "content_hash": "H2"},
            ]
        }
    )

    assert summary == {
        "artifact_count": 3,
        "unique_media_count": 2,
        "unique_content_hash_count": 2,
        "duplicate_reference_count": 1,
        "duplicate_group_count": 1,
        "duplicate_groups": [
            {
                "media_id": "M1",
                "content_hash": "H1",
                "count": 2,
                "labels": ["PG001_film_00.png", "PG001_film_01.png"],
            }
        ],
    }






@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["list", "--help"],
        ["show", "--help"],
        ["archive", "--help"],
        ["history", "--help"],
        ["diff", "--help"],
    ],
)
def test_timelines_help_is_executable(argv: list[str]) -> None:
    from astrid.packs.timeline.cli import build_parser

    parser = build_parser(_FakeClient())
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# T28 — gateway dispatch cutover for the timelines family
# ---------------------------------------------------------------------------


def test_dispatch_timelines_routes_product_verbs_through_product_dispatch(
    monkeypatch,
) -> None:
    from astrid.core.gateway import dispatch

    seen: dict[str, object] = {}

    def _fake_product(args):  # noqa: ANN001
        seen["args"] = list(args)
        return 7

    monkeypatch.setattr(dispatch, "_dispatch_product", _fake_product)
    assert dispatch._dispatch_timelines(["list", "--project", "demo"]) == 7
    assert seen["args"] == ["timelines", "list", "--project", "demo"]


def test_dispatch_timelines_has_no_legacy_cli_fallback(monkeypatch) -> None:
    """m6 teardown: every timelines route goes through the product boundary.

    ``astrid.core.cli.timeline`` is deleted, so there is no legacy CLI to
    fall back to — even a formerly-legacy verb (e.g. ``visualize``) is
    forwarded to ``_dispatch_product`` (the family parser rejects it).
    """
    from astrid.core.gateway import dispatch

    seen: dict[str, object] = {}

    def _fake_product(args):  # noqa: ANN001
        seen["args"] = list(args)
        return 3

    monkeypatch.setattr(dispatch, "_dispatch_product", _fake_product)
    assert dispatch._dispatch_timelines(["visualize", "--project", "demo"]) == 3
    assert seen["args"] == ["timelines", "visualize", "--project", "demo"]

    # The legacy timeline CLI module no longer exists anywhere in the tree.
    import importlib

    with pytest.raises(ImportError):
        importlib.import_module("astrid.core.cli.timeline")


# ---------------------------------------------------------------------------
# T29 — nested shots family beneath timelines
# ---------------------------------------------------------------------------


def test_shots_parser_has_exactly_eight_verbs_beneath_timelines() -> None:
    from astrid.packs.shots.cli import COMMANDS, build_parser

    assert tuple(spec.name for spec in COMMANDS) == (
        "text",
        "list",
        "show",
        "create",
        "add",
        "remove",
        "reorder",
    )
    assert all(spec.aliases == () for spec in COMMANDS)
    parser = build_parser(_FakeClient())
    assert parser.prog == "astrid timelines shots"
    assert _subparser_choices(parser) == {
        "text",
        "list",
        "show",
        "create",
        "add",
        "remove",
        "reorder",
    }


def test_no_top_level_shots_family() -> None:
    """shots is not a product family: product dispatch rejects it."""
    from astrid.core.cli.domain_product import (
        PRODUCT_FAMILY_SET,
        ProductRegistryError,
        is_product_family,
    )
    from astrid.core.gateway import dispatch

    assert "shots" not in PRODUCT_FAMILY_SET
    assert not is_product_family("shots")
    assert "shots" not in dispatch._top_level_commands()
    client = _FakeClient()
    with pytest.raises(ProductRegistryError, match="not a product family"):
        _run("shots", ["list", "--project", "demo"], client=client)
    assert client.calls == []


def test_timelines_shots_routes_beneath_timelines_parser(capsys) -> None:
    """``timelines shots`` is reachable only through the timelines parser."""
    client = _FakeClient()
    rc = _run(
        "timelines",
        ["shots", "list", "--project", "demo", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [("shots.list", {"project": "demo"})]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True


def test_shots_show_is_one_sdk_call_with_ordered_media_mapping(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        ["shots", "show", "S-1", "--project", "demo", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [("shots.show", {"project": "demo", "shot_id": "S-1"})]
    envelope = json.loads(capsys.readouterr().out)
    item = envelope["data"]["items"][0]
    assert item["id"] == "I-1"
    assert item["media_id"] == "M-1"
    assert item["position"] == 0
    assert item["media"]["path"] == "/tmp/opening.png"


def test_shots_create_is_one_sdk_call_with_exact_envelope(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        [
            "shots",
            "create",
            "--project",
            "demo",
            "--name",
            "Opening",
            "--metadata",
            '{"take": 1}',
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "shots.create",
            {
                "project": "demo",
                "name": "Opening",
                "metadata": {"take": 1},
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"]["id"] == "S-1"
    assert envelope["receipt"]["command_kind"] == "shot.create"
    assert envelope["idempotency_key"] == "generated-key"


def test_shots_create_forwards_caller_key() -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        [
            "shots",
            "create",
            "--project",
            "demo",
            "--name",
            "Opening",
            "--idempotency-key",
            "shot-key",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls[0][1]["idempotency_key"] == "shot-key"


def test_shots_add_is_one_sdk_call_with_media_position_and_key(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        [
            "shots",
            "add",
            "--project",
            "demo",
            "S-1",
            "--media",
            "M-1",
            "--position",
            "0",
            "--source-frame",
            "12",
            "--idempotency-key",
            "add-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "shots.add_item",
            {
                "project": "demo",
                "shot_id": "S-1",
                "media_id": "M-1",
                "position": 0,
                "source_frame": 12,
                "metadata": None,
                "idempotency_key": "add-key",
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "shot.add_item"
    assert envelope["idempotency_key"] == "add-key"


def test_shots_add_requires_media() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("timelines", ["shots", "add", "--project", "demo", "S-1"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_shots_remove_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        ["shots", "remove", "--project", "demo", "S-1", "I-1", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "shots.remove_item",
            {
                "project": "demo",
                "shot_id": "S-1",
                "item_id": "I-1",
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "shot.remove_item"


def test_shots_reorder_is_one_sdk_call_with_item_permutation(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "timelines",
        [
            "shots",
            "reorder",
            "--project",
            "demo",
            "S-1",
            "--items",
            "I-2,I-1",
            "--items",
            "I-3",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "shots.reorder",
            {
                "project": "demo",
                "shot_id": "S-1",
                "item_ids": ["I-2", "I-1", "I-3"],
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "shot.reorder"


def test_shots_reorder_requires_items() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("timelines", ["shots", "reorder", "--project", "demo", "S-1"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_shots_failure_envelope_exits_one(capsys) -> None:
    class _FailingShots(_RecordingShots):
        def list(self, project):
            self._owner.calls.append(("shots.list", {"project": project}))
            return DomainResult.failure(
                ErrorObject(code="not_found", message="missing", details={})
            )

    class _FailingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.shots = _FailingShots(self)

    client = _FailingClient()
    rc = _run("timelines", ["shots", "list", "--project", "demo"], client=client)
    assert rc == 1
    assert len(client.calls) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "not_found"
    assert json.loads(captured.out)["error"]["message"] == "missing"


def test_shots_unknown_verb_is_a_usage_error() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("timelines", ["shots", "export", "--project", "demo"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["shots", "--help"],
        ["shots", "list", "--help"],
        ["shots", "show", "--help"],
        ["shots", "create", "--help"],
        ["shots", "add", "--help"],
        ["shots", "remove", "--help"],
        ["shots", "reorder", "--help"],
    ],
)
def test_timelines_shots_help_is_executable(argv: list[str]) -> None:
    from astrid.packs.timeline.cli import build_parser

    parser = build_parser(_FakeClient())
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)
    assert excinfo.value.code == 0


def test_dispatch_timelines_shots_routes_through_product_dispatch(
    monkeypatch,
) -> None:
    from astrid.core.gateway import dispatch

    seen: dict[str, object] = {}

    def _fake_product(args):  # noqa: ANN001
        seen["args"] = list(args)
        return 9

    monkeypatch.setattr(dispatch, "_dispatch_product", _fake_product)
    assert dispatch._dispatch_timelines(["shots", "list", "--project", "demo"]) == 9
    assert seen["args"] == ["timelines", "shots", "list", "--project", "demo"]


def test_projects_list_default_matches_explicit_json(capsys) -> None:
    client = _FakeClient()
    assert _run("projects", ["list"], client=client) == 0
    default = capsys.readouterr().out
    assert _run("projects", ["list", "--json"], client=client) == 0
    assert default == capsys.readouterr().out
    assert json.loads(default)["ok"] is True


def test_timelines_render_review_is_public_admitted_input(capsys):
    client = _FakeClient()
    assert _run('timelines', ['render', 'main', '--project', 'demo', '--review', '--json'], client=client) == 0
    assert client.calls[0][1]['inputs'] == {'timeline_ref': 'main', 'review': True}


def test_timelines_visualize_forwards_filmstrip_options(capsys) -> None:
    client = _FakeClient()
    rc = _run('timelines', [
        'visualize', '--project', 'demo', 'main', '--view', 'filmstrip',
        '--sample', 'cuts', '--render-run', '01EXACT', '--every-frames', '12',
        '--columns', '4', '--page-size', '40', '--range', '10..20', '--json',
    ], client=client)
    assert rc == 0
    assert len(client.calls) == 1
    assert client.calls[0][1]["wait"] is True
    assert client.calls[0][1]['inputs'] == {
        'formats': ['png', 'md'], 'timeline_slug': 'main', 'view': 'filmstrip',
        'sample': 'cuts', 'render_run': '01EXACT', 'every_frames': 12,
        'columns': 4, 'page_size': 40, 'range': '10..20',
    }
    capsys.readouterr()
