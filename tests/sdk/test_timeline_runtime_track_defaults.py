"""SDK timeline creation inherits tracks from the current Runtime bootstrap."""
from __future__ import annotations

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("config", "expected_tracks"),
    [
        (None, [("V1", "visual"), ("A1", "audio")]),
        (
            {"theme_overrides": {"visual": {"canvas": {
                "width": 640, "height": 360, "fps": 24,
            }}}},
            [("V1", "visual"), ("A1", "audio")],
        ),
        ({"tracks": []}, []),
    ],
    ids=("no-config", "canvas-only", "explicit-empty-tracks"),
)
def test_create_empty_preserves_runtime_tracks_unless_caller_supplies_tracks(
    real_runtime, config, expected_tracks,
):
    """The SDK leaves Runtime-owned bootstrap tracks alone unless tracks are explicit."""
    use_runtime()
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    daemon = real_runtime(authoring_profile="astrid")
    try:
        project = daemon.service.create_project({
            "slug": "track-defaults", "name": "Track defaults",
        })["id"]
        client = open_client(daemon)
        created = client.timelines.create_empty(
            project=project,
            timeline_id="film",
            config=config,
            idempotency_key="track-defaults",
        )
        assert created.ok, created.as_dict()
        payload = created.data["canonical_document"]["parent_revision"]["payload"]
        tracks = payload["config"]["tracks"]
        assert [(track["id"], track["kind"]) for track in tracks] == expected_tracks
        if config and "theme_overrides" in config:
            assert payload["config"]["output"] == {
                "resolution": "640x360", "fps": 24, "file": "output.mp4",
                "background": None, "background_scale": None,
            }
    finally:
        daemon.stop()
