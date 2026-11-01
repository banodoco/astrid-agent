"""GenericPackHost acceptance for the real Remotion render capability.

The test renders with the repository's own provisioned Remotion install
(``npm ci --prefix remotion``, which the lanes workflow runs).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers.remotion_runtime import remotion_install_problems
from tests.packs.rendering._helpers import _execution_env

ROOT = Path(__file__).resolve().parents[2]
# The pinned timeline-schema dependency is installed with the dev extras; its install root is the schema root.
SCHEMA_ROOT = Path(importlib.util.find_spec("banodoco_timeline_schema").origin).resolve().parent.parent
REMOTION = ROOT / "remotion"

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient  # noqa: E402
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


@pytest.fixture
def remotion_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Select the provisioned server-owned Remotion install."""

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
