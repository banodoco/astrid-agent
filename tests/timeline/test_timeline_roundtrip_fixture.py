"""Round-trip fixture test pinning timeline JSON byte-equivalence.

The regression gate for the `Timeline` domain class. Three assertions:

1. `examples/hype.timeline.full.json` round-trips load -> dump
   byte-for-byte (covers tracks, theme_overrides, generation_defaults,
   per-clip animation/transition/effects, mixed clipTypes).
2. Unknown top-level fields survive load/dump via the passthrough bag.
3. Astrid's Python allowlists (`_TIMELINE_TOP_ALLOWED`, `_CLIP_ALLOWED`,
   `_TRACK_ALLOWED`) match the canonical Runtime-vendored TimelineConfig JSON
   Schema, with Astrid's documented top-level `app` and clip-level
   render-provenance extension metadata retained around the renderable shape.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_PATH = REPO_ROOT / "examples" / "hype.timeline.full.json"

_ASTRID_TOP_LEVEL_OVERLAY = frozenset({"app"})
_ASTRID_CLIP_OVERLAY = frozenset({"shot_id", "shot_occurrence_id", "shot_name", "elementRef"})


# Make the in-tree astrid package importable when running pytest from
# the repo root (it already is) — defensive, costs nothing.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrid.core.timeline import (  # noqa: E402
    _CLIP_ALLOWED,
    _TIMELINE_TOP_ALLOWED,
    _TRACK_ALLOWED,
    load_timeline,
    save_timeline,
)
from astrid.core.timeline.banodoco_schema import (  # noqa: E402
    load_vendored_timeline_schema,
)


class TimelineRoundTripFixtureTest(unittest.TestCase):
    """Phase 2 regression gate."""

    def setUp(self) -> None:
        self.assertTrue(
            FIXTURE_PATH.is_file(),
            f"fixture missing: {FIXTURE_PATH}",
        )
        self.original_text = FIXTURE_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # 1. Byte-equivalent round-trip
    # ------------------------------------------------------------------
    def test_round_trip_is_byte_equivalent(self) -> None:
        config = load_timeline(FIXTURE_PATH)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "roundtrip.json"
            save_timeline(config, out_path)
            roundtripped = out_path.read_text(encoding="utf-8")

        if self.original_text != roundtripped:
            # Surface a JSON-normalised diff so it's obvious what shifted.
            original_norm = json.dumps(
                json.loads(self.original_text), indent=2, sort_keys=True
            )
            roundtrip_norm = json.dumps(
                json.loads(roundtripped), indent=2, sort_keys=True
            )
            self.assertEqual(
                original_norm,
                roundtrip_norm,
                "Round-trip changed timeline content (key order normalised for diff).",
            )
            # Same JSON content, byte mismatch => formatting drift.
            self.fail(
                "Round-trip preserved JSON content but not exact bytes "
                "(formatting drift in save_timeline). Phase 2 must keep "
                "byte-equivalence."
            )

        # Belt-and-braces: every section we care about survived as JSON.
        loaded = json.loads(roundtripped)
        original = json.loads(self.original_text)
        for section in (
            "theme",
            "theme_overrides",
            "tracks",
            "clips",
        ):
            self.assertEqual(
                loaded.get(section),
                original.get(section),
                f"section {section!r} drifted after round-trip",
            )

    def test_track_roles_survive_round_trip(self) -> None:
        for role in ("output", "source"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                injected = json.loads(self.original_text)
                injected["tracks"][0]["role"] = role
                in_path = Path(tmp) / "with_role.json"
                in_path.write_text(json.dumps(injected, indent=2) + "\n", encoding="utf-8")

                config = load_timeline(in_path)
                out_path = Path(tmp) / "after_roundtrip.json"
                save_timeline(config, out_path)
                after = json.loads(out_path.read_text(encoding="utf-8"))

                self.assertEqual(after, injected)
                self.assertEqual(after["tracks"][0]["role"], role)

    # ------------------------------------------------------------------
    # 2. Unknown top-level field preservation
    # ------------------------------------------------------------------
    def test_unknown_top_level_field_is_preserved(self) -> None:
        sentinel_key = "_phase2_canary"
        sentinel_value = "preserve me"

        injected = json.loads(self.original_text)
        injected[sentinel_key] = sentinel_value

        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "with_canary.json"
            in_path.write_text(json.dumps(injected, indent=2), encoding="utf-8")

            config = load_timeline(in_path)

            out_path = Path(tmp) / "after_roundtrip.json"
            save_timeline(config, out_path)
            after = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(
            after.get(sentinel_key),
            sentinel_value,
            "unknown top-level field must survive load/dump (passthrough bag).",
        )

    # ------------------------------------------------------------------
    # 3. Allowlist parity with the canonical Runtime-vendored schema
    # ------------------------------------------------------------------
    def test_allowlist_parity_with_runtime_vendored_schema(self) -> None:
        schema = load_vendored_timeline_schema()
        schema_origin = (
            REPO_ROOT
            / "astrid"
            / "core"
            / "timeline"
            / "schema"
            / "timeline-config.schema.json"
        )

        defs = schema.get("definitions") or schema.get("$defs") or {}
        # Plan-v5 B2: TimelineConfig is the schema ROOT (the old $ref-root was
        # restructured); fall back to the root when no definition is present.
        timeline_def = defs.get("TimelineConfig") or schema
        clip_def = defs.get("TimelineClip")
        self.assertIsNotNone(timeline_def, "TimelineConfig missing in Runtime-vendored schema")
        self.assertIsNotNone(clip_def, "TimelineClip missing in Runtime-vendored schema")

        shared_top = set((timeline_def.get("properties") or {}).keys())
        shared_clip = set((clip_def.get("properties") or {}).keys())

        # TrackDefinition is inlined under TimelineConfig.tracks.items.
        tracks_node = (timeline_def.get("properties") or {}).get("tracks") or {}
        track_items = tracks_node.get("items") or {}
        shared_track = set((track_items.get("properties") or {}).keys())

        # The Runtime-vendored schema is the source of truth. Astrid retains the
        # documented top-level editor metadata overlay and the clip-level
        # render-admission provenance overlay around the shape.
        expected_top = shared_top | _ASTRID_TOP_LEVEL_OVERLAY
        self.assertEqual(
            set(_TIMELINE_TOP_ALLOWED),
            expected_top,
            "Timeline top-level allowlist drift between Astrid "
            "(_TIMELINE_TOP_ALLOWED) and Runtime-vendored schema (TimelineConfig); "
            f"schema-origin={schema_origin}; "
            f"only-in-astrid={set(_TIMELINE_TOP_ALLOWED) - expected_top}, "
            f"only-in-schema={shared_top - set(_TIMELINE_TOP_ALLOWED)}",
        )
        # ``derived_output`` is a runtime-produced result envelope accepted
        # by the shared schema, not an authoring field Astrid admits from an
        # editor. The three ``shot_*`` fields are an explicit Astrid
        # render-admission provenance overlay and are stripped before shared
        # validation by ``_known_timeline_payload``.
        self.assertEqual(
            set(_CLIP_ALLOWED),
            (shared_clip - {"derived_output"}) | _ASTRID_CLIP_OVERLAY,
            "Clip allowlist drift between Astrid (_CLIP_ALLOWED) and "
            "the supported subset of Runtime-vendored schema (TimelineClip); "
            f"schema-origin={schema_origin}; "
            f"only-in-astrid={set(_CLIP_ALLOWED) - (shared_clip | _ASTRID_CLIP_OVERLAY)}, "
            f"only-in-schema={shared_clip - set(_CLIP_ALLOWED) - {'derived_output'}}",
        )
        self.assertEqual(
            set(_TRACK_ALLOWED),
            shared_track,
            "Track allowlist drift between Astrid (_TRACK_ALLOWED) and "
            "Runtime-vendored schema (TimelineConfig.tracks[]); "
            f"schema-origin={schema_origin}; "
            f"only-in-astrid={set(_TRACK_ALLOWED) - shared_track}, "
            f"only-in-schema={shared_track - set(_TRACK_ALLOWED)}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
