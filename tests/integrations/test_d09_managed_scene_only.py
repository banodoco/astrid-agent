"""D09 proof through Runtime admission, the real pack host, and Scene."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from astrid.core.execution.generic_host import (
    GenericPackHost,
    RuntimeProtocolClient,
    _launch_worker_probe_environments,
)
from astrid.core.timeline.authoring_bundle import open_authoring_bundle, preview_authoring_candidate
from astrid.sdk import AstridClient, timeline_filmstrip
from astrid.sdk.pagination import paged_rows
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline_checkout import Checkout

ROOT = Path(__file__).resolve().parents[2]
REMOTION = Path(
    os.environ.get(
        "ASTRID_REMOTION_PROJECT_DIR",
        "/home/user/work/runtimes/agent-remotion-composition14-255b6856",
    )
).resolve()
NODE = Path("/home/user/work/toolchain/node-v20.19.4-linux-x64/bin/node").resolve()
SCHEMA = Path("/home/user/work/venv-agent/lib/python3.13/site-packages").resolve()
_APPROVED_REMOTION_PACKAGE = json.loads(
    (ROOT / "remotion" / "package.json").read_text(encoding="utf-8")
)
_APPROVED_REMOTION_LOCK = json.loads(
    (ROOT / "remotion" / "package-lock.json").read_text(encoding="utf-8")
)
_APPROVED_COMPOSITION_DEPENDENCY = _APPROVED_REMOTION_PACKAGE["dependencies"][
    "@banodoco/timeline-composition"
]
_APPROVED_COMPOSITION_ARTIFACT = _APPROVED_COMPOSITION_DEPENDENCY.removeprefix("file:")
_APPROVED_COMPOSITION_SHA256 = (
    ROOT / "remotion" / f"{_APPROVED_COMPOSITION_ARTIFACT}.sha256"
).read_text(encoding="utf-8").strip()
_APPROVED_COMPOSITION_LOCK = _APPROVED_REMOTION_LOCK["packages"][
    "node_modules/@banodoco/timeline-composition"
]
PACK_ROOTS = (
    ROOT / "astrid" / "packs" / "rendering",
    ROOT / "astrid" / "packs" / "astrid_motion",
    ROOT / "astrid" / "packs" / "local",
)


def _assert_current_source_imports() -> None:
    """Keep the live proof tied to the committed candidate source tree."""
    from astrid.core.rendering import scene_answers, verified_source
    from astrid.packs.rendering.actions.timeline_visualize import filmstrip_execution, scene_only
    from astrid.packs.rendering.rendering.renderers.remotion import root_preparation

    modules = {
        "sdk": timeline_filmstrip,
        "executor": filmstrip_execution,
        "scene_only": scene_only,
        "scene_answers": scene_answers,
        "root_preparation": root_preparation,
        "verified_source": verified_source,
    }
    for name, module in modules.items():
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(ROOT), f"{name} imported outside candidate tree: {path}"
    assert ROOT.resolve() in {Path(entry or ".").resolve() for entry in sys.path}

# This node is selected only by the explicit central live test request.  It is
# intentionally not marked ``integration`` because the bounded targeted lane
# otherwise applies the repository's default ``not integration`` expression.


@pytest.fixture(autouse=True)
def _public13_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin this proof to the read-only public13 installed profile."""
    _assert_current_source_imports()
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    monkeypatch.delenv("NODE_PATH", raising=False)
    for name in tuple(os.environ):
        if name.startswith("GIT_") and name not in {
            "GIT_EDITOR",
            "GIT_SEQUENCE_EDITOR",
            "GIT_PAGER",
            "GIT_TERMINAL_PROMPT",
            "GIT_ASKPASS",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_SSH_VARIANT",
            "GIT_MERGE_AUTOEDIT",
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        }:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "ASTRID_RUNTIME_CHECKOUT",
        os.environ.get("ASTRID_RUNTIME_CHECKOUT", "/home/user/work/runtime"),
    )
    private_profile = tmp_path / "current-composition-profile"
    shutil.copytree(
        REMOTION,
        private_profile,
        symlinks=True,
        ignore=shutil.ignore_patterns("node_modules"),
    )
    shutil.copytree(
        REMOTION / "node_modules",
        private_profile / "node_modules",
        symlinks=False,
    )
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(private_profile))
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", str(NODE))
    monkeypatch.setenv(
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH",
        os.environ.get("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", str(SCHEMA)),
    )
    monkeypatch.setenv("ASTRID_PACKS_PATH", os.pathsep.join(str(root) for root in PACK_ROOTS))
    assert private_profile.is_dir()
    assert NODE.is_file() and os.access(NODE, os.X_OK)
    package = json.loads((private_profile / "package.json").read_text(encoding="utf-8"))
    assert _APPROVED_COMPOSITION_DEPENDENCY == _APPROVED_COMPOSITION_LOCK["resolved"]
    assert package["dependencies"]["@banodoco/timeline-composition"] == (
        _APPROVED_COMPOSITION_DEPENDENCY
    )
    lock = json.loads((private_profile / "package-lock.json").read_text(encoding="utf-8"))
    installed_lock = lock["packages"]["node_modules/@banodoco/timeline-composition"]
    assert installed_lock["resolved"] == _APPROVED_COMPOSITION_DEPENDENCY
    assert installed_lock["integrity"] == _APPROVED_COMPOSITION_LOCK["integrity"]
    artifact = private_profile / _APPROVED_COMPOSITION_ARTIFACT
    assert artifact.is_file()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == _APPROVED_COMPOSITION_SHA256
    assert artifact.stat().st_size == (
        ROOT / "remotion" / _APPROVED_COMPOSITION_ARTIFACT
    ).stat().st_size
    installed_package = private_profile / "node_modules/@banodoco/timeline-composition"
    with tarfile.open(artifact) as archive:
        regular_members = [member for member in archive.getmembers() if member.isfile()]
        assert len(regular_members) == 80
        manifest_file = archive.extractfile("package/package.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)
        assert manifest["version"] == _APPROVED_COMPOSITION_LOCK["version"]
        expected_files = {member.name.removeprefix("package/") for member in regular_members}
        actual_files = {
            path.relative_to(installed_package).as_posix()
            for path in installed_package.rglob("*")
            if path.is_file()
        }
        assert actual_files == expected_files
        for member in regular_members:
            target = installed_package / member.name.removeprefix("package/")
            member_file = archive.extractfile(member)
            assert member_file is not None
            assert target.read_bytes() == member_file.read()
    from astrid.core.rendering.remotion_runtime import prepare_remotion_validation

    worker_contexts = _launch_worker_probe_environments(
        PACK_ROOTS,
        dict(os.environ),
        ROOT / "config" / "astrid-beta-capabilities.json",
        remotion_only=True,
    )

    validation = prepare_remotion_validation(
        artifact_directory=tmp_path / "remotion-validation",
        host_environment=dict(os.environ),
        python_executable=sys.executable,
        launch_id=uuid.uuid4().hex,
        contexts={"host": dict(os.environ), "worker": worker_contexts},
    )
    for name, value in validation.items():
        monkeypatch.setenv(name, value)


def _connect(daemon: Any) -> AstridClient:
    return AstridClient.open(
        endpoint=daemon.endpoint,
        credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"],
        actor_id="owner",
        client_name="d09-managed-scene-test",
        client_version="test",
        protocol_version="workspace.v1",
    )


def _publish(client: AstridClient, *, suffix: str) -> tuple[str, str]:
    transport = client._remote._transport
    created = transport.create_project(
        f"D09 {suffix}", idempotency_key=f"d09-project-{suffix}", slug=f"d09-{suffix}"
    )
    project = created.get("data", created)
    project_id = str(project.get("project_id") or project.get("id"))
    transport.create_timeline(project_id, "main", idempotency_key=f"d09-timeline-{suffix}")
    opened = client.timelines.open_bundle(project_id, "main")
    assert opened.ok, opened.as_dict()

    def commit(commands: list[dict[str, Any]], key: str) -> Any:
        head = client.timelines.history(project_id, "main", limit=10)
        assert head.ok, head.as_dict()
        pair = head.data["head"]
        body = {
            "api_version": 1,
            "project_id": project_id,
            "timeline_id": "main",
            "base_revision_id": pair["revision_id"],
            "base_publication_id": pair["publication_id"],
            "commands": commands,
        }
        result = client.timelines.commit(project_id, "main", body, idempotency_key=key)
        assert result.ok, result.as_dict()
        return result

    track = capture_track_create(opened.data["bundle"], "parent", {
        "id": "picture", "kind": "visual", "role": "output", "label": "Picture",
    })
    commit([track], f"d09-track-{suffix}")
    opened = client.timelines.open_bundle(project_id, "main")
    assert opened.ok, opened.as_dict()
    clip = capture_clip_add(opened.data["bundle"], "parent", {
        "id": "title",
        "track": "picture",
        "clipType": "text",
        "at": 0,
        "hold": 1,
        "text": {"content": "saved"},
    })
    result = commit([clip], f"d09-clip-{suffix}")
    return project_id, str(result.data["accepted_revision_id"])


def _host(daemon: Any, tmp_path: Path) -> GenericPackHost:
    worker = RuntimeProtocolClient(
        daemon.endpoint,
        daemon.worker_credential_path.read_text(encoding="utf-8").strip(),
        credential_file=daemon.worker_credential_path,
    )
    host = GenericPackHost(
        pack_roots=list(PACK_ROOTS),
        client=worker,
        executor_id="astrid-pack-host",
        attempt_root=tmp_path / "attempts",
    )
    records = {record.id: record for record in host.discover()}
    assert "rendering.render" in records
    assert "rendering.timeline_visualize" in records
    host.preflight("rendering.render")
    host.preflight("rendering.timeline_visualize")
    assert host.capabilities["rendering.render"].ready
    assert host.capabilities["rendering.timeline_visualize"].ready
    registration = host.register()
    assert registration["registration"].executor_id == "astrid-pack-host"
    return host


def _run_one(host: GenericPackHost, operation: Any, *, max_tasks: int = 1) -> Any:
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            host.run(max_tasks=max_tasks, poll_seconds=0.05)
        except Exception as exc:  # noqa: BLE001 - surface host failure in the test
            errors.append(exc)

    thread = threading.Thread(target=serve, name="d09-pack-host")
    thread.start()
    try:
        result = operation()
    finally:
        if thread.is_alive():
            host.shutdown()
        thread.join(timeout=30)
    assert not thread.is_alive(), "pack host did not settle the managed task"
    assert not errors, errors[0] if errors else None
    return result


def _latest_scene_task(client: AstridClient, project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tasks = paged_rows(client._remote._transport.list_project_tasks, project_id, limit=50)
    assert tasks
    rows = [row for row in tasks if isinstance(row, dict)]
    scene = next(
        row for row in reversed(rows)
        if row.get("capability", row.get("capability_id")) == "rendering.timeline_visualize"
        and row.get("state") in {"succeeded", "completed"}
    )
    task_id = str(scene.get("task_id") or scene.get("id"))
    managed = client.tasks.list_managed_outputs(task_id)
    assert managed.ok and managed.data
    output_rows = managed.data[0] if isinstance(managed.data, (list, tuple)) else managed.data
    assert isinstance(output_rows, list)
    return scene, [row for row in output_rows if isinstance(row, dict)]


def _assert_managed_scene_bundle(client: AstridClient, rows: list[dict[str, Any]]) -> dict[str, Any]:
    bundles = [row for row in rows if row.get("output_port") == "filmstrip_bundle"]
    assert len(bundles) == 1
    row = bundles[0]
    digest = str(row["digest"])
    data = client.media.read_bytes(digest)
    assert len(data) == row["size"]
    assert hashlib.sha256(data).hexdigest() == digest.removeprefix("sha256:")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["manifest.json", "scene-answers.json"]
        manifest = json.loads(archive.read("manifest.json"))
        payload = json.loads(archive.read("scene-answers.json"))
    assert manifest["kind"] == "timeline_filmstrip"
    assert payload["answers"]
    assert payload["scene_clips"]["title"]["address"] == "title"
    return {"row": row, "manifest": manifest, "payload": payload}


@pytest.mark.parametrize("candidate_mode", [False, True], ids=["saved", "candidate"])
def test_saved_and_frozen_candidate_scene_only_use_real_managed_bundle(
    tmp_path: Path, real_runtime: Any, candidate_mode: bool
) -> None:
    daemon = real_runtime(authoring_profile="astrid")
    with _connect(daemon) as client:
        project_id, parent_revision = _publish(client, suffix="positive")
        request: dict[str, Any] = {"timeline_ref": "main"}
        if candidate_mode:
            parent = client._remote._transport.get_project_parent_composition_revision(
                project_id, "main", parent_revision
            )
            preview_bundle = open_authoring_bundle(
                parent, shot_revisions=[], internal_timeline_revisions=[]
            )
            preview_bundle["parent"]["clips"][0]["text"]["content"] = "candidate"
            preview_bundle["parent"]["config"]["clips"][0]["text"]["content"] = "candidate"
            request["authoring_preview"] = preview_authoring_candidate(preview_bundle)
        host = _host(daemon, tmp_path)
        spans, context = _run_one(
            host,
            lambda: timeline_filmstrip._invoke_scene_only(
                request, project=project_id, client=client
            ),
        )
        assert spans[0].cls != "UNAVAILABLE", vars(spans[0])
        assert spans[0].clips == ("title",)
        _, rows = _latest_scene_task(client, project_id)
        bundle = _assert_managed_scene_bundle(client, rows)
        expected_status = "candidate" if candidate_mode else "committed"
        assert bundle["payload"]["snapshot_label"]["status"] == expected_status
        if candidate_mode:
            assert bundle["payload"]["snapshot_label"]["base_revision_id"] == parent_revision
        else:
            stale_context = dict(context)
            stale_context["capture_identity"] = "sha256:" + "0" * 64
            stale_spans = timeline_filmstrip._scene_only_spans(
                {"ok": True, "outputs": {"managed_outputs": [bundle["row"]]}},
                stale_context,
                client=client,
            )
            assert stale_spans[0].cls == "UNAVAILABLE"
            assert stale_spans[0].findings


def test_public_checkout_check_uses_retained_managed_scene_result(
    tmp_path: Path, real_runtime: Any
) -> None:
    """The ordinary public check entry uses the managed Scene result."""
    daemon = real_runtime(authoring_profile="astrid")
    with _connect(daemon) as client:
        project_id, parent_revision = _publish(client, suffix="public-check")
        host = _host(daemon, tmp_path)
        checkout = Checkout.open(project_id, "main", client=client)

        # Checkout.check() is the ordinary public zero-argument entry.  It
        # receives no private Scene inputs, callback, or fabricated source.
        report = _run_one(host, checkout.check)
        assert report.valid

        scene, rows = _latest_scene_task(client, project_id)
        assert scene.get("capability", scene.get("capability_id")) == "rendering.timeline_visualize"
        bundle = _assert_managed_scene_bundle(client, rows)
        assert bundle["row"]["digest"].startswith("sha256:")
        assert bundle["manifest"]["entrypoints"]["scene"] == "scene-answers.json"
        assert bundle["payload"]["scene_clips"]["title"]["address"] == "title"
        assert bundle["payload"]["snapshot_label"] == {
            "status": "committed",
            "label": f"committed revision {parent_revision}",
            "actual_revision_id": parent_revision,
            "base_revision_id": None,
        }
        # The real public13 evaluator may honestly return UNKNOWN for a
        # component without evaluated bounds.  The managed result was bound
        # successfully; only missing/stale transport is UNAVAILABLE.
        assert not any(line.startswith("scene   UNAVAILABLE") for line in report.problems), report.problems
