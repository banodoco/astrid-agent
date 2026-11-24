"""GenericPackHost acceptance for the real Remotion render capability.

The test renders with the repository's own provisioned Remotion install
(``npm ci --prefix remotion``, which the lanes workflow runs).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.env_vars import (  # noqa: E402
    ASTRID_REMOTION_VALIDATION_LAUNCH,
    ASTRID_REMOTION_VALIDATION_PATH,
    ASTRID_REMOTION_VALIDATION_ROLE,
    ASTRID_REMOTION_VALIDATION_SHA256,
)
from astrid.sdk.host_bootstrap import _prepare_render_validation  # noqa: E402
from tests.helpers.remotion_runtime import remotion_install_problems
from tests.packs.rendering._helpers import _execution_env

ROOT = Path(__file__).resolve().parents[2]
# The canonical schema is vendored from the exact Runtime source pin.
SCHEMA_ROOT = ROOT / "astrid/core/timeline/schema/timeline-config.schema.json"
REMOTION = ROOT / "remotion"
_REMOTION_UNSUPPORTED_OVERRIDES = (
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "PYTHONHOME",
    "NODE_OPTIONS",
    "NODE_PATH",
    "NODE_ICU_DATA",
    "OPENSSL_CONF",
    "PYTHONUSERBASE",
    "PYTHONNOUSERSITE",
    "PYTHONSAFEPATH",
    "PYTHONPYCACHEPREFIX",
    "PYTHONPLATLIBDIR",
    "PYTHONCASEOK",
    "PYTHONUTF8",
    "PYTHONCOERCECLOCALE",
    "PYTHONSTARTUP",
)

from astrid.core.execution.generic_host import (  # noqa: E402
    GenericPackHost,
    HostError,
    RuntimeProtocolClient,
)
from astrid.core.theme import builtin_theme  # noqa: E402
from astrid.packs.rendering.rendering.renderers.remotion.root_preparation import (  # noqa: E402
    _prepared_remotion_root,
)
from astrid.packs.rendering.shared.rendering_backend import (  # noqa: E402
    _resolved_theme_for_render,
)
from astrid.sdk.client import AstridClient  # noqa: E402
from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR  # noqa: E402
from astrid_runtime_client import WorkspaceClient  # noqa: E402

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)


def _make_media(root: Path) -> tuple[Path, Path]:
    media = root / "media"
    media.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(media / "black.mp4"),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            str(media / "silence.m4a"),
        ],
        check=True,
        capture_output=True,
    )
    black = (media / "black.mp4").read_bytes()
    silence = (media / "silence.m4a").read_bytes()
    (media / "assets.json").write_text(
        json.dumps(
            {
                "assets": {
                    "black": {
                        "media_id": "runtime-black",
                        "content_sha256": hashlib.sha256(black).hexdigest(),
                        "type": "video/mp4",
                        "duration": 2.0,
                        "resolution": "160x90",
                        "fps": 10,
                    },
                    "silence": {
                        "media_id": "runtime-silence",
                        "content_sha256": hashlib.sha256(silence).hexdigest(),
                        "type": "audio/mp4",
                        "duration": 2.0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    timeline = {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {
                "canvas": {"width": 160, "height": 90, "fps": 10},
                "color": {"fg": "#ffffff", "bg": "#000000", "accent": "#ffffff"},
            }
        },
        "tracks": [
            {"id": "source", "kind": "visual", "label": "Source"},
            {"id": "audio", "kind": "audio", "label": "Audio"},
        ],
        "clips": [
            {
                "id": "source_black",
                "at": 0,
                "track": "source",
                "clipType": "media",
                "asset": "black",
                "from": 0,
                "to": 0.6,
                "speed": 1,
                "volume": 0,
            },
            {
                "id": "audio_silence",
                "at": 0,
                "track": "audio",
                "clipType": "media",
                "asset": "silence",
                "from": 0,
                "to": 0.6,
                "speed": 1,
                "volume": 1,
            },
        ],
    }
    timeline_path = media / "timeline.json"
    timeline_path.write_text(json.dumps(timeline))
    return timeline_path, media / "assets.json"


def _prepare_remotion_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    """Prepare the theme proof's temporary media fixture around the provisioned runtime."""

    for name in list(os.environ):
        if name.startswith(("DYLD_", "LD_")) or name in _REMOTION_UNSUPPORTED_OVERRIDES:
            monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_root = workspace / "astrid"
    source_root.mkdir()
    pack_root = source_root / "packs"
    pack_root.symlink_to(ROOT / "astrid" / "packs", target_is_directory=True)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(REMOTION.resolve()))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(pack_root.resolve()))
    env = dict(os.environ)
    launch = _prepare_render_validation(
        ROOT,
        env,
        tmp_path / "support",
        sys.executable,
        (pack_root,),
    )
    assert launch is not None
    for name in (
        ASTRID_REMOTION_VALIDATION_PATH,
        ASTRID_REMOTION_VALIDATION_SHA256,
        ASTRID_REMOTION_VALIDATION_LAUNCH,
        ASTRID_REMOTION_VALIDATION_ROLE,
    ):
        monkeypatch.setenv(name, env[name])
    timeline_path, assets_path = _make_media(workspace)
    return workspace, REMOTION, timeline_path, assets_path


@pytest.fixture
def remotion_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Select the provisioned Remotion install and its owned host/worker validation."""

    problems = remotion_install_problems()
    if problems:
        pytest.fail(
            "a real render needs the provisioned Remotion install (`npm ci --prefix remotion`): " + "; ".join(problems),
            pytrace=False,
        )
    node = shutil.which("node")
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", str(Path(node).resolve()))
    monkeypatch.setenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", str(SCHEMA_ROOT))
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(REMOTION.resolve()))
    with _execution_env():
        for name in list(os.environ):
            if name.startswith(("DYLD_", "LD_")) or name in _REMOTION_UNSUPPORTED_OVERRIDES:
                monkeypatch.delenv(name, raising=False)
        # Use the same launch transaction as the real pack host.  It qualifies
        # the host and the worker projection; a standalone-only artifact does
        # not match GenericPackHost's worker readiness context.
        env = dict(os.environ)
        launch = _prepare_render_validation(
            ROOT,
            env,
            tmp_path / "support",
            sys.executable,
            (ROOT / "astrid" / "packs" / "rendering",),
        )
        assert launch is not None
        for name in [
            key
            for key in os.environ
            if key not in env and key.startswith(("DYLD_", "LD_"))
        ]:
            monkeypatch.delenv(name)
        for name in (
            ASTRID_REMOTION_VALIDATION_PATH,
            ASTRID_REMOTION_VALIDATION_SHA256,
            ASTRID_REMOTION_VALIDATION_LAUNCH,
            ASTRID_REMOTION_VALIDATION_ROLE,
        ):
            monkeypatch.setenv(name, env[name])
        yield REMOTION


def test_generic_host_remotion_register_claim_execute_settle_and_cas(
    tmp_path: Path, real_runtime, remotion_launch: Path
) -> None:
    remotion = remotion_launch
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    timeline_path, assets_path = _make_media(workspace)

    daemon = real_runtime()
    try:
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        generated.handshake(
            "generic-remotion-acceptance",
            "0.1.0",
            [
                "projects:read",
                "projects:write",
                "objects:read",
                "objects:write",
                "tasks:read",
                "tasks:write",
            ],
        )
        project = generated.create_project(
            "Generic Remotion acceptance",
            slug="generic-remotion-acceptance",
            idempotency_key="generic-remotion-project",
        )
        project_id = str(project["project_id"])
        # The host owns the only path handoff. Import both sources into the
        # runtime, then replace the fixture's stable placeholders with the
        # returned runtime identities/digests before the task is admitted.
        registry = json.loads(assets_path.read_text(encoding="utf-8"))
        input_object_ids: list[str] = []
        for key, filename in (("black", "black.mp4"), ("silence", "silence.m4a")):
            imported = generated.ingest_project_object(
                project_id,
                (workspace / "media" / filename).read_bytes(),
                media_type=registry["assets"][key]["type"],
                idempotency_key=f"generic-remotion-{key}",
                filename=filename,
            )
            object_id = imported.get("object_id") if isinstance(imported, dict) else getattr(imported, "object_id", None)
            digest = imported.get("digest") if isinstance(imported, dict) else getattr(imported, "digest", None)
            assert isinstance(object_id, str) and isinstance(digest, str)
            input_object_ids.append(object_id)
            registry["assets"][key]["media_id"] = object_id
            registry["assets"][key]["content_sha256"] = digest.removeprefix("sha256:")
        assets_path.write_text(json.dumps(registry), encoding="utf-8")
        # File inputs reach the executor only as managed Runtime objects, never as caller-local paths.
        managed: dict[str, dict[str, str]] = {}
        for name, path in (("timeline", timeline_path), ("assets_registry", assets_path)):
            imported = generated.ingest_project_object(
                project_id,
                path.read_bytes(),
                media_type="application/json",
                idempotency_key=f"generic-remotion-{name}",
                filename=path.name,
            )
            object_id = imported.get("object_id") if isinstance(imported, dict) else getattr(imported, "object_id", None)
            digest = imported.get("digest") if isinstance(imported, dict) else getattr(imported, "digest", None)
            assert isinstance(object_id, str) and isinstance(digest, str)
            input_object_ids.append(object_id)
            managed[name] = {"object_id": object_id, "digest": digest}
        pack = ROOT / "astrid" / "packs" / "rendering"
        host_client = RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
        host = GenericPackHost(
            pack_roots=[pack],
            client=host_client,
            executor_id="astrid-pack-host",
            attempt_root=tmp_path / "attempt",
        )
        record = next(r for r in host.discover() if r.id == "rendering.render")
        host.preflight(record.id)
        assert host.capabilities[record.id].ready is True

        registration = host.register()
        assert registration["registration"].executor_id == "astrid-pack-host"

        spec = {
            "inputs": {
                "timeline": managed["timeline"],
                "assets_registry": managed["assets_registry"],
                "selector": "rendering.remotion",
                "backend_config": {
                    "rendering.remotion": {
                        "project_dir": str(remotion),
                        "composition_id": "TimelineComposition",
                    }
                },
                "output_name": "generic.mp4",
            }
        }
        task = generated.admit_task(
            capability_id=record.id,
            capability_digest=record.capability_digest,
            input_object_ids=input_object_ids,
            idempotency_key="generic-remotion-task",
            project_id=project_id,
            spec=spec,
        )
        # The host's own client claims, so it records the attempt's Runtime epoch that run_task fences on.
        claim = host_client.claim_next(
            executor_id="astrid-pack-host",
            capability_ids=[record.id],
            idempotency_key="generic-remotion-claim",
        )
        assert claim is not None and claim["task_id"] == task.task_id

        # The task document the claim loop hands run_task also carries the run and the Runtime epoch.
        claimed_task = host_client.task(task.task_id)
        settled = host.run_task(
            {
                "task": {
                    "run_id": claimed_task.run_id,
                    "runtime_epoch": getattr(claimed_task, "runtime_epoch", None) or claim["runtime_epoch"],
                    "id": task.task_id,
                    "capability": record.id,
                    "spec": claim.spec,
                    "input_object_ids": claim.input_object_ids,
                    "project_id": claim.project_id,
                    "attempt_id": claim["attempt_id"],
                    "fence": claim["fence"],
                }
            },
            lease_token=claim["lease_id"],
            attempt_id=claim["attempt_id"],
            fence=int(claim["fence"]),
        )
        assert settled.state == "succeeded"
        assert generated.get_task(task.task_id).state == "succeeded"

        outputs_dir = tmp_path / "attempt" / "outputs"
        output = outputs_dir / "generic.mp4"
        provenance = outputs_dir / "generic.mp4.provenance.json"
        assert output.is_file() and output.stat().st_size > 0
        assert provenance.is_file() and provenance.stat().st_size > 0
        for path in (output, provenance):
            digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            assert generated.get_object(digest).data == path.read_bytes()
    finally:
        daemon.stop()


def _rn11_frame_colour(video: Path, seconds: str) -> tuple[int, int, int]:
    # Average the whole frame: the two authored layers are solid colours, so
    # this cannot pass just because one corner or an unrelated title is drawn.
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", seconds, "-i", str(video),
         "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout
    assert len(raw) == 3, f"RN-11: missing frame at {seconds}s"
    return raw[0], raw[1], raw[2]


def test_short_managed_contract_builtin_and_fade_through_command_worker(
    tmp_path: Path, real_runtime, remotion_launch: Path
) -> None:
    """One short product-authored render, including visible built-in and fade.

    The explicit non-linear easing must survive the RN-6 declaration/admission
    path. The pinned composition package does not consume fade easing (#1739);
    these pixels prove a visible fade, not the non-linear curve's semantics.
    RN-4 must land before adding the collection/project-pack cases.
    """
    from uuid import uuid4

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline._clip_commands import capture_clip_add
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from tests.helpers.runtime import vendored_runtime_commit

    daemon = real_runtime(commit=vendored_runtime_commit(), authoring_profile="astrid")
    host = None
    try:
        owner = open_client(daemon)
        project_result = owner.projects.create(
            slug="rn11-render-contract", name="RN-11 render contract",
            idempotency_key="rn11-project",
        )
        assert project_result.ok, project_result.as_dict()
        project_id = project_result.data["project_id"]

        listed = owner.timelines.list(project_id)
        assert listed.ok, listed.as_dict()
        rows, cursor = listed.data
        assert not cursor, listed.data
        if rows:
            assert len(rows) == 1, rows
            timeline_id = rows[0]["timeline_id"]
        else:
            timeline_id = str(uuid4())
            created = owner.timelines.create_empty(
                project=project_id, timeline_id=timeline_id, name="RN-11",
                config={"theme_overrides": {"visual": {"canvas": {
                    "width": 320, "height": 180, "fps": 10,
                }}}},
                idempotency_key="rn11-timeline",
            )
            assert created.ok, created.as_dict()

        timeline = Timeline.open(
            project_id, timeline_id, client=owner, outbox_path=tmp_path / "rn11-outbox.json",
        )
        # Generate our own media and let the real SDK import and bind it, rather
        # than fabricating media IDs, digest fields or object locators.
        background = tmp_path / "rn11-background.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0xe8452c:s=320x180:r=1",
             "-frames:v", "1", "-threads", "1", "-c:v", "png", str(background)],
            check=True, capture_output=True,
        )

        def place_background(t):
            t.track("titles", above="V1")
            t.add(background, at=0, hold=2.2, track="V1", id="source")

        imported = timeline.commit(place_background, label="RN-11 managed background", key="rn11-media")
        assert imported["receipt"]["receipt_id"]
        entrance = {"type": "fade", "duration": 0.8, "params": {"easing": "out"}}
        row = {
            "id": "builtin", "at": 0.4, "hold": 1.4, "track": "titles",
            "clipType": "audio-reactive-colour",
            "params": {"schemaVersion": 1, "initialColor": "#2ecc71", "events": []},
            "entrance": entrance,
        }
        published = timeline.commit(
            [capture_clip_add(timeline.bundle, "parent", row)],
            label="RN-11 built-in with easing", key="rn11-element",
        )
        assert published["receipt"]["receipt_id"]
        revision = published["data"]["accepted_revision_id"]
        parent = published["data"]["canonical_document"]["parent_revision"]["payload"]
        tracks = [track["id"] for track in parent["config"]["tracks"]]
        assert tracks.index("titles") < tracks.index("V1"), tracks
        clips = {clip["id"]: clip for clip in parent["clips"]}
        assert clips["builtin"]["entrance"] == entrance
        assert clips["builtin"]["clipType"] == "audio-reactive-colour"
        source = parent["registry"]["assets"][clips["source"]["asset"]]
        assert source["media_id"] == "sha256:" + hashlib.sha256(background.read_bytes()).hexdigest()

        host = GenericPackHost(
            pack_roots=[ROOT / "astrid" / "packs" / "rendering"],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id=PACK_HOST_ACTOR,
            attempt_root=tmp_path / "attempts",
        )
        host.discover()
        host.preflight("rendering.render")
        record = host.capabilities["rendering.render"]
        assert record.ready and record.preflight["remotion"]["ok"] is True
        host.register(deliberate=True)

        # Admit by canonical timeline reference, not a caller-local file or an
        # injected materialized registry. The host must run the real worker.
        admitted = owner.tasks.create(
            project_id=project_id, capability="rendering.render",
            capability_digest=record.capability_digest,
            input_manifest=[], idempotency_key="rn11-render",
            spec={
                "family": "rendering.render",
                "params": {"timeline_ref": timeline_id, "selector": "rendering.remotion",
                           "output_name": "rn11.mp4"},
                "output_policy": {"format": "mp4", "destination": "download", "filename": "rn11.mp4"},
            },
        )
        assert admitted.ok, admitted.as_dict()
        task_id = admitted.data["task_id"]
        settled = host.run(once=True)
        assert len(settled) == 1
        shown = owner.tasks.show(task_id)
        assert shown.ok, shown.as_dict()
        task = shown.data
        assert task["state"] == "succeeded", task
        authority = task["spec"]["spec"]["inputs"]["timeline_authority"]
        assert authority["parent_revision_id"] == revision
        result = task["result"]
        selection = result["renderer_selection"]
        assert selection["selector"] == "rendering.remotion"
        assert selection["remotion_ok"] is True
        videos = [item for item in result["outputs"]
                  if str(item.get("media_type", "")).startswith("video/")]
        assert len(videos) == 1, result
        digest = videos[0]["digest"]
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        generated.handshake("rn11-output", "1", ["objects:read"])
        content = generated.get_object(digest).data
        assert content and digest == "sha256:" + hashlib.sha256(content).hexdigest()
        output = tmp_path / "rn11.mp4"
        output.write_bytes(content)

        before = _rn11_frame_colour(output, "0.2")
        during = _rn11_frame_colour(output, "0.8")
        after = _rn11_frame_colour(output, "1.5")
        tail = _rn11_frame_colour(output, "2.0")
        # Tolerate the encoded video's colour conversion, not blank frames,
        # wrong layer order, missing animation, or missing/truncated media.
        for sample in (before, tail):
            assert all(abs(a - b) <= 16 for a, b in zip(sample, (232, 69, 44))), sample
        assert all(abs(a - b) <= 16 for a, b in zip(after, (46, 204, 113))), after
        assert after[0] + 20 < during[0] < before[0] - 20, (before, during, after)
        assert before[1] + 20 < during[1] < after[1] - 20, (before, during, after)
    finally:
        if host is not None:
            host.shutdown()
        daemon.stop()


def test_generic_host_normal_imported_theme_reaches_props_and_rejects_local_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_runtime
) -> None:
    """Prove SDK admission through named materialization into Root props.

    This is intentionally a prefix proof: it does not execute or settle a
    final render, and therefore claims no completed task, MP4, browser, or
    parity result.
    """
    import astrid.sdk as sdk

    workspace, remotion, timeline_path, assets_path = _prepare_remotion_fixture(
        tmp_path, monkeypatch
    )
    # The ordinary file caller cannot authorize assets named inside a registry.
    # Use the media-free title film from test_remotion_backend's real-render
    # fixture, without authored overrides that would mask the theme controls.
    timeline_path.write_text(
        json.dumps(
            {
                "theme": "banodoco-default",
                "tracks": [{"id": "v1", "kind": "visual", "label": "Title"}],
                "clips": [
                    {
                        "id": "title",
                        "at": 0.0,
                        "track": "v1",
                        "clipType": "text",
                        "hold": 0.5,
                        "text": {
                            "content": "Remotion ANGLE",
                            "fontSize": 64,
                            "color": "#ffffff",
                        },
                        "params": {"weight": 700},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assets_path.write_text(json.dumps({"assets": {}}), encoding="utf-8")
    daemon = real_runtime()
    try:
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        generated.handshake(
            "generic-remotion-theme-proof",
            "0.1.0",
            [
                "projects:read",
                "projects:write",
                "objects:read",
                "objects:write",
                "tasks:read",
                "tasks:write",
            ],
        )
        project = generated.create_project(
            "Generic Remotion theme proof",
            slug="generic-remotion-theme-proof",
            idempotency_key="generic-remotion-theme-project",
        )
        project_id = str(project["project_id"])

        owner = AstridClient.open(
            endpoint=daemon.endpoint,
            credential=daemon.credential_path,
            realm_id=daemon.service.realm["id"],
            actor_id="owner",
            client_name="generic-remotion-theme-proof-owner",
            client_version="test",
            protocol_version="workspace.v1",
        )
        explicit_theme = json.loads(json.dumps(builtin_theme()))
        explicit_theme["id"] = "imported-explicit-theme"
        explicit_theme["visual"]["color"] = {
            "fg": "#e8f1ff",
            "bg": "#26113d",
            "accent": "#ff8a3d",
        }
        theme_source = workspace / "media" / "explicit-theme.json"
        theme_source.write_text(json.dumps(explicit_theme), encoding="utf-8")
        timeline_import = owner.media.import_file(
            project=project_id,
            path=timeline_path,
            idempotency_key="generic-remotion-theme-timeline",
        )
        assert timeline_import.ok
        registry_import = owner.media.import_file(
            project=project_id,
            path=assets_path,
            idempotency_key="generic-remotion-theme-assets",
        )
        assert registry_import.ok
        imported = owner.media.import_file(
            project=project_id,
            path=theme_source,
            idempotency_key="generic-remotion-explicit-theme",
        )
        assert imported.ok
        descriptor = imported.data

        host = GenericPackHost(
            pack_roots=[workspace / "astrid" / "packs"],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id=PACK_HOST_ACTOR,
            attempt_root=tmp_path / "attempt",
        )
        record = next(item for item in host.discover() if item.id == "rendering.render")
        assert "theme" in {port.name for port in record.definition.inputs}
        host.preflight(record.id)
        assert host.capabilities[record.id].ready is True
        registration = host.register()
        assert registration["registration"].executor_id == PACK_HOST_ACTOR

        admissions = []
        create_task = owner.tasks.create

        def capture_admission(**request):
            response = create_task(**request)
            admissions.append((request, response))
            return response

        monkeypatch.setattr(owner.tasks, "create", capture_admission)
        # The private pack tree is a host-launch fixture input.  Leaving it in
        # the caller environment makes SDK discovery combine it with the
        # checkout's implicit source tree, duplicating h3_av.transform.
        with monkeypatch.context() as invoke_env:
            invoke_env.delenv("ASTRID_PACKS_PATH", raising=False)
            result = sdk.invoke(
                "rendering.render",
                kind="executor",
                project=project_id,
                inputs={
                    "timeline": {
                        "object_id": timeline_import.data["object_id"],
                        "digest": timeline_import.data["digest"],
                        "filename": "hype.timeline.json",
                    },
                    "assets_registry": {
                        "object_id": registry_import.data["object_id"],
                        "digest": registry_import.data["digest"],
                        "filename": "hype.assets.json",
                    },
                    "theme": {
                        "object_id": descriptor["object_id"],
                        "digest": descriptor["digest"],
                        "filename": "theme.json",
                    },
                    "selector": "rendering.remotion",
                },
                client=owner,
            )
        assert result.ok, result.to_dict()
        assert len(admissions) == 1
        request, response = admissions[0]
        assert response.ok
        task = generated.get_task(result.kernel_task_id)
        assert task.input_object_ids == request["input_manifest"]
        admitted_inputs = task.spec["spec"]["inputs"]
        assert admitted_inputs == request["spec"]["inputs"]
        print(
            json.dumps(
                {
                    "sdk_admission": request,
                    "admission_result": response.data,
                    "sdk_result": result.to_dict(),
                },
                default=str,
            )
        )
        attempt = tmp_path / "materialized-attempt"
        values = host._materialize_inputs(
            task.spec,
            attempt,
            authorized_input_object_ids=task.input_object_ids,
            file_input_names=frozenset(
                port.name for port in record.definition.inputs if port.type == "file"
            ),
        )
        materialized_theme = Path(values["theme"])
        assert materialized_theme == Path(values["materialized_objects"]["theme"])
        assert materialized_theme.name == "theme.json"
        assert materialized_theme.is_relative_to(attempt / "managed-objects")
        assert json.loads(materialized_theme.read_text(encoding="utf-8")) == explicit_theme

        # Root preparation is the endpoint here; the staged video is only the
        # required output placeholder for this pre-render props inspection.
        staged_video = tmp_path / "prepared" / "unused.mp4"
        with _prepared_remotion_root(
            Path(values["timeline"]),
            Path(values["assets_registry"]),
            staged_video,
            provenance_out_path=staged_video,
            project_dir=remotion,
            composition_id="TimelineComposition",
            theme_path=materialized_theme,
            min_free_gb=None,
            materialized_root=Path(values["materialized_root"]),
            materialized_objects=values["materialized_objects"],
            staging_parent=tmp_path / "prepared",
            frame_numbers=(0,),
            frame_output_dir=tmp_path / "frames",
        ) as prepared:
            props = prepared[0]
            assert props["theme"]["id"] == explicit_theme["id"]
            assert props["theme"]["visual"]["color"] == explicit_theme["visual"]["color"]

        omitted = _resolved_theme_for_render(Path(values["timeline"]), None)
        builtin = builtin_theme()
        assert omitted == {"id": builtin["id"], "visual": builtin["visual"]}
        assert omitted["id"] != explicit_theme["id"]

        forged = tmp_path / "forged-theme.json"
        forged.write_text(json.dumps(explicit_theme), encoding="utf-8")
        forged_spec = {"spec": {"inputs": {**admitted_inputs, "theme": str(forged)}}}
        with pytest.raises(HostError, match="requires a managed Runtime object"):
            host._materialize_inputs(
                {**forged_spec, "input_object_ids": task.input_object_ids},
                tmp_path / "forged-attempt",
                file_input_names=frozenset(
                    port.name for port in record.definition.inputs if port.type == "file"
                ),
            )
    finally:
        daemon.stop()


def test_worker_validation_reports_changed_pack_root_without_other_values(
    tmp_path: Path, remotion_launch: Path,
) -> None:
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    from astrid.core.rendering import remotion_runtime

    launch_environment = dict(os.environ)
    candidates = _launch_worker_probe_environments(
        (ROOT / 'astrid' / 'packs' / 'rendering',), launch_environment,
        remotion_only=True, dispatch_only=True,
    )
    worker = candidates[0]
    worker[ASTRID_REMOTION_VALIDATION_ROLE] = 'worker'
    remotion_runtime._validated_runtime(remotion_launch, environment=worker)
    changed_root = tmp_path / 'changed-packs'
    changed_root.mkdir()
    worker['ASTRID_PACKS_PATH'] = str(changed_root)
    worker['PRIVATE_SENTINEL'] = 'unrelated-value-must-not-appear'
    with pytest.raises(remotion_runtime.RemotionValidationError, match='ASTRID_PACKS_PATH') as error:
        remotion_runtime._validated_runtime(remotion_launch, environment=worker)
    assert str(changed_root) in str(error.value)
    assert 'unrelated-value-must-not-appear' not in str(error.value)


@pytest.mark.installed
def test_installed_captured_command_render_through_host(tmp_path: Path, monkeypatch, real_runtime) -> None:
    import astrid
    from astrid.core.execution import generic_host

    site = Path(astrid.__file__).resolve().parent.parent
    assert sys.flags.isolated
    assert os.environ.get('ASTRID_HOST_EXECUTION_PROFILE') == 'installed'
    assert site != ROOT
    remotion = Path(os.environ['ASTRID_REMOTION_PROJECT_DIR']).resolve(strict=True)
    node = Path(shutil.which('node')).resolve(strict=True)
    monkeypatch.setenv('ASTRID_NODE_EXECUTABLE', str(node))
    monkeypatch.setenv('ASTRID_TIMELINE_SCHEMA_PYTHONPATH',
                       str(site / 'astrid/core/timeline/schema/timeline-config.schema.json'))
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'runtime-data'))
    monkeypatch.setattr(sys.modules[__name__], 'ROOT', site)
    monkeypatch.setattr(generic_host, '_select_pack_capture', lambda: generic_host._portable_pack_capture)
    with _execution_env():
        environment = dict(os.environ)
        launch = _prepare_render_validation(
            site, environment, tmp_path / 'support', sys.executable,
            (site / 'astrid/packs/rendering',),
        )
        assert launch is not None
        for name in (ASTRID_REMOTION_VALIDATION_PATH, ASTRID_REMOTION_VALIDATION_SHA256,
                     ASTRID_REMOTION_VALIDATION_LAUNCH, ASTRID_REMOTION_VALIDATION_ROLE):
            monkeypatch.setenv(name, environment[name])
        test_generic_host_remotion_register_claim_execute_settle_and_cas(tmp_path, real_runtime, remotion)
