"""Bounded Gate 5 fixture: import media before authoring a timeline.

This uses only the public Runtime media-import and verified-media reads.  The
fresh realm is intentionally still empty of timelines when the old selection
is obtained; App can then use this selection while authoring its initial film.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


def _fixture_image() -> bytes:
    """Produce one locally decodable image without a checked-in media blob."""
    if shutil.which("ffmpeg") is None:
        pytest.fail("ffmpeg is required for the normal media-import fixture")
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=red:s=16x16:r=1", "-frames:v", "1", "-threads", "1",
            "-c:v", "png", "-f", "image2pipe", "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def test_gate5_normal_import_yields_exact_old_media_before_authoring(tmp_path, monkeypatch):
    """The seeded placeholder must be authored from a settled managed output."""
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / "packages" / "python"))
    from runtime_protocol.daemon import RuntimeDaemon
    from runtime_protocol.store import RealmStore

    from astrid_runtime_client import WorkspaceClient

    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    daemon = RuntimeDaemon(realm, support_root=tmp_path / "support", authoring_profile="astrid").start()
    try:
        client = WorkspaceClient(daemon.endpoint, daemon.token)
        project = client.create_project(
            "Gate 5 media fixture", slug="gate5-media", idempotency_key="gate5-project"
        )
        project_id = project["project_id"]
        timelines, next_cursor = client.list_timelines(project_id)
        assert timelines == []
        assert next_cursor is None
        image = _fixture_image()

        # This is the normal public admission path.  The Runtime creates the
        # task, generation, variant, and catalog entry; the fixture fabricates
        # none of those and has not authored a timeline yet.
        imported = client.import_project_media(
            project_id,
            image,
            media_type="image/png",
            filename="f2-placeholder.png",
            width=16,
            height=16,
            idempotency_key="gate5-placeholder-import",
        )
        outputs, next_cursor = client.list_managed_outputs(imported["task_id"])
        assert next_cursor is None
        assert len(outputs) == 1
        output = outputs[0]
        assert output.durability == "durable"
        assert output.lifecycle["state"] in {"available", "promoted"}

        old_media = client.get_project_media_selection(
            project_id,
            {
                "association_id": output.association_id,
                "object_id": output.object_id,
                "version": output.lifecycle["version"],
            },
        )
        assert set(old_media) == {
            "asset", "generation_id", "variant_id", "object_id", "catalog_digest",
        }
        assert old_media["generation_id"] == imported["generation_id"]
        assert old_media["variant_id"] == imported["variant_id"]
        assert old_media["object_id"] == imported["asset_id"]

        verified = client.get_project_verified_media(project_id, old_media)
        assert verified["selection"] == old_media
        assert verified["media_type"] == "image/png"
        assert verified["size"] == len(image)
        assert verified["width"] == 16
        assert verified["height"] == 16
    finally:
        daemon.stop()
