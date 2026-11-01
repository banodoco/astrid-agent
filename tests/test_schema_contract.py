import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from astrid.core import timeline
from astrid.core.timeline import banodoco_schema

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
GENERATOR = ROOT / "scripts" / "gen_remotion_types.py"


class SchemaContractTest(unittest.TestCase):
    maxDiff = None

    def _make_tempdir(self, prefix: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def _roundtrip_timeline(self, path: Path) -> str:
        original = path.read_text(encoding="utf-8")
        config = timeline.load_timeline(path)
        tmp_path = self._make_tempdir("timeline-roundtrip-") / path.name
        timeline.save_timeline(config, tmp_path)
        return tmp_path.read_text(encoding="utf-8"), original

    def _roundtrip_registry(self, path: Path) -> str:
        original = path.read_text(encoding="utf-8")
        registry = timeline.load_registry(path)
        tmp_path = self._make_tempdir("registry-roundtrip-") / path.name
        timeline.save_registry(registry, tmp_path)
        return tmp_path.read_text(encoding="utf-8"), original

    def _generate_types(self) -> str:
        tmp_path = self._make_tempdir("generated-types-") / "types.generated.ts"
        subprocess.run([sys.executable, str(GENERATOR), str(tmp_path)], cwd=ROOT, check=True)
        return tmp_path.read_text(encoding="utf-8")

    def _parse_generated_array(self, name: str) -> set[str]:
        source = self._generate_types()
        pattern = rf"export const {name} = \[(.*?)\] as const;"
        match = re.search(pattern, source, re.DOTALL)
        self.assertIsNotNone(match, f"Missing generated array {name}")
        return set(re.findall(r"'([^']+)'", match.group(1)))

    def test_golden_roundtrip(self) -> None:
        roundtripped_timeline, original_timeline = self._roundtrip_timeline(EXAMPLES / "hype.timeline.json")
        roundtripped_assets, original_assets = self._roundtrip_registry(EXAMPLES / "hype.assets.json")
        self.assertEqual(roundtripped_timeline, original_timeline)
        self.assertEqual(roundtripped_assets, original_assets)

    def test_golden_timeline_teaches_concern_tracks(self) -> None:
        config = timeline.load_timeline(EXAMPLES / "hype.timeline.json")
        visual_tracks = [track["id"] for track in config["tracks"] if track.get("kind") == "visual"]
        self.assertEqual(visual_tracks, ["brand", "captions", "broll", "v1"])

        clips_by_track: dict[str, list[dict]] = {}
        for clip in config["clips"]:
            clips_by_track.setdefault(clip["track"], []).append(clip)

        self.assertEqual(clips_by_track["brand"][0]["id"], "brand_wordmark")
        self.assertEqual(clips_by_track["captions"][0]["id"], "cap_search")
        self.assertEqual(clips_by_track["broll"][0]["id"], "broll_cutaway")
        self.assertEqual(clips_by_track["v1"][0]["id"], "src_open")

    def test_full_roundtrip(self) -> None:
        roundtripped_timeline, original_timeline = self._roundtrip_timeline(EXAMPLES / "hype.timeline.full.json")
        roundtripped_assets, original_assets = self._roundtrip_registry(EXAMPLES / "hype.assets.full.json")
        self.assertEqual(roundtripped_timeline, original_timeline)
        self.assertEqual(roundtripped_assets, original_assets)

    def test_generator_byte_stability(self) -> None:
        self.assertEqual(self._generate_types(), self._generate_types())

    def test_generator_emits_each_typescript_identifier_once(self) -> None:
        source = self._generate_types()
        names = re.findall(
            r"^export (?:interface|type|const) ([A-Za-z_$][A-Za-z0-9_$]*)",
            source,
            re.MULTILINE,
        )
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        self.assertEqual(duplicates, [])

    def test_generator_arrays_match_frozensets(self) -> None:
        self.assertEqual(self._parse_generated_array("_TIMELINE_TOP_ALLOWED"), set(timeline._TIMELINE_TOP_ALLOWED))
        self.assertEqual(self._parse_generated_array("_THEME_OVERRIDES_ALLOWED"), set(timeline._THEME_OVERRIDES_ALLOWED))
        self.assertEqual(self._parse_generated_array("_CLIP_ALLOWED"), set(timeline._CLIP_ALLOWED))
        self.assertEqual(self._parse_generated_array("_TRACK_ALLOWED"), set(timeline._TRACK_ALLOWED))
        self.assertEqual(self._parse_generated_array("_ASSET_ENTRY_ALLOWED"), set(timeline._ASSET_ENTRY_ALLOWED))

    def test_full_fixture_covers_optional_keys(self) -> None:
        config = timeline.load_timeline(EXAMPLES / "hype.timeline.full.json")
        registry = timeline.load_registry(EXAMPLES / "hype.assets.full.json")

        # Exercise the optional Runtime 0.0.2 track role on this local copy so
        # the shared characterization fixture keeps its original digest input.
        config["tracks"][0]["role"] = "output"

        self.assertIn("generation_defaults", timeline._TIMELINE_TOP_ALLOWED)
        clip_keys = {key for clip in config["clips"] for key in timeline._normalize_clip_for_validation(clip)}
        track_keys = {key for track in config.get("tracks", []) for key in track}
        theme_overrides_keys = set(config.get("theme_overrides") or {})
        asset_entry_keys = {key for entry in registry["assets"].values() for key in entry}

        # The full fixture exercises the shared authoring shape. Astrid's
        # shot-provenance overlay is admitted for render-admission outputs,
        # but is intentionally not present on every authoring clip.
        self.assertEqual(
            clip_keys,
            set(timeline._CLIP_ALLOWED)
            - {"app", "derived_output", "elementRef", "keyframes", "label", "shot_id", "shot_occurrence_id", "shot_name"},
        )
        self.assertEqual(track_keys, set(timeline._TRACK_ALLOWED) - {"app"})
        self.assertEqual(theme_overrides_keys, set(timeline._THEME_OVERRIDES_ALLOWED))
        self.assertEqual(asset_entry_keys, set(timeline._ASSET_ENTRY_ALLOWED))

        text_keys = set(config["clips"][2]["text"])
        self.assertEqual(
            text_keys,
            {"content", "fontFamily", "fontSize", "color", "align", "bold", "italic"},
        )
        self.assertEqual(
            set(config["clips"][0]["entrance"]),
            {"type", "duration", "intensity", "params"},
        )
        self.assertEqual(
            set(config["clips"][0]["exit"]),
            {"type", "duration", "intensity", "params"},
        )
        self.assertEqual(
            set(config["clips"][0]["continuous"]),
            {"type", "intensity", "params"},
        )
        self.assertEqual(
            set(config["clips"][0]["transition"]),
            {"type", "duration"},
        )
        self.assertEqual(
            set(config["clips"][0]["effects"]),
            {"fade_in", "fade_out"},
        )

    def test_editor_app_extensions_roundtrip_through_shared_validator(self) -> None:
        config = {
            "app": {"haus.astrid.scene-phase-markers": {"sceneMarkers": []}},
            "tracks": [
                {
                    "id": "visual",
                    "kind": "visual",
                    "label": "Visual",
                    "app": {"scaleAppliesToPositionedClips": True},
                }
            ],
            "clips": [
                {
                    "id": "title",
                    "at": 0,
                    "track": "visual",
                    "clipType": "text",
                    "text": {"content": "hello"},
                    "app": {"editorOnly": True},
                }
            ],
        }

        # ``app`` is opaque editor metadata: the upstream schema rejects it,
        # but Astrid must preserve it while validating the renderable shape.
        self.assertEqual(timeline.canonical_timeline_config(config), config)

    def test_generation_defaults_roundtrip_preserves_nested_object(self) -> None:
        config = {
            "theme": "banodoco-default",
            "theme_overrides": {"visual": {"canvas": {"fps": 24}}},
            "generation_defaults": {
                "model": "sequence-v1",
                "image": {"quality": "high", "provider": "astrid"},
                "provider_settings": {"seed": 1234, "flags": ["keep", "open"]},
            },
            "tracks": [],
            "clips": [],
        }
        path = self._make_tempdir("generation-defaults-") / "timeline.json"

        timeline.save_timeline(config, path)  # type: ignore[arg-type]
        loaded = timeline.load_timeline(path)

        self.assertEqual(loaded["generation_defaults"], config["generation_defaults"])

    def test_generation_defaults_validation_does_not_inspect_inner_keys(self) -> None:
        config = {
            "theme": "banodoco-default",
            "generation_defaults": {
                "model": "sequence-v1",
                "image": {"quality": "high"},
            },
            "tracks": [],
            "clips": [],
        }

        timeline.validate_timeline(config, strict=False)

    def test_validate_timeline_accepts_missing_theme(self) -> None:
        # Persisted timelines may omit `theme`; renderable default is injected
        # at render time via Timeline.for_render(), never written back.
        timeline.validate_timeline({"clips": [], "tracks": []}, strict=False)

    def test_container_validation_preserves_editor_app_metadata(self) -> None:
        config = {
            "clips": [],
            "tracks": [],
            "app": {"com.example.extension": {"marker": "kept"}},
        }

        validated = timeline.validate_timeline_config_for_container(config)

        # ``app`` is Astrid/editor extension metadata, not an upstream render
        # schema field. It must survive round-trip while remaining excluded
        # from the strict shared-schema payload.
        self.assertEqual(validated["app"], config["app"])

    def test_canonical_empty_timeline_is_raw_runtime_container(self) -> None:
        config = timeline.canonical_empty_timeline()

        self.assertEqual(config, {"clips": [], "tracks": []})
        timeline.validate_timeline_config_for_container(config)

        config["clips"].append({"id": "mutated"})  # type: ignore[attr-defined]
        self.assertEqual(timeline.canonical_empty_timeline(), {"clips": [], "tracks": []})

    def test_container_validation_rejects_empty_and_legacy_shapes(self) -> None:
        invalid_shapes = [
            {},
            {"clips": []},
            {"tracks": []},
            {"schema_version": 1, "assembly": {"clips": [], "tracks": []}},
            {"clips": [], "tracks": [], "pool": {"entries": []}},
            {"clips": [], "tracks": [], "arrangement": {"clips": []}},
        ]

        for config in invalid_shapes:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    timeline.validate_timeline_config_for_container(config)

    def test_container_validation_returns_json_safe_copy_without_mutating_input(self) -> None:
        config = {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Video"}],
            "clips": [
                {
                    "id": "c1",
                    "at": 0,
                    "track": "v1",
                    "clipType": "text",
                    "hold": 2,
                    "text": {"content": "Hello"},
                }
            ],
        }
        original = {
            "tracks": [dict(config["tracks"][0])],
            "clips": [dict(config["clips"][0])],
        }

        validated = timeline.validate_timeline_config_for_container(config)
        validated["tracks"][0]["label"] = "Changed"  # type: ignore[index]
        validated["clips"][0]["text"]["content"] = "Changed"  # type: ignore[index]

        self.assertEqual(config, original)

    def test_canonical_timeline_config_digest_is_stable_and_caller_safe(self) -> None:
        config = {
            "clips": [],
            "tracks": [{"kind": "visual", "label": "Video", "id": "v1"}],
            "theme_overrides": {"visual": {"canvas": {"fps": 24, "width": 1920}}},
        }
        reordered = {
            "theme_overrides": {"visual": {"canvas": {"width": 1920, "fps": 24}}},
            "tracks": [{"label": "Video", "id": "v1", "kind": "visual"}],
            "clips": [],
        }

        before = str(config)
        self.assertTrue(timeline.timeline_configs_equal(config, reordered))
        self.assertEqual(
            timeline.timeline_config_digest(config),
            timeline.timeline_config_digest(reordered),
        )
        self.assertEqual(str(config), before)

    def test_validate_timeline_rejects_empty_or_non_string_theme(self) -> None:
        with self.assertRaisesRegex(ValueError, "Timeline.theme must be a non-empty slug"):
            timeline.validate_timeline({"theme": "", "clips": [], "tracks": []}, strict=False)
        with self.assertRaisesRegex(ValueError, "Timeline.theme must be a non-empty slug"):
            timeline.validate_timeline({"theme": 42, "clips": [], "tracks": []}, strict=False)

    def test_effect_params_accept_animation_reference_arrays(self) -> None:
        config = {
            "theme": "banodoco-default",
            "tracks": [],
            "clips": [
                {
                    "id": "title",
                    "at": 0,
                    "track": "v1",
                    "clipType": "text-card",
                    "hold": 2,
                    "params": {
                        "content": "Hello",
                        "entrance": ["fade-up", {"id": "type-on", "durationFrames": 12}],
                        "exit": {"id": "fade", "durationFrames": 8, "params": {"opacity": 0}},
                    },
                }
            ],
        }

        def meta(animation_id: str) -> dict:
            return {
                "fade-up": {"kind": "wrapper", "phase": "entrance"},
                "type-on": {"kind": "hook", "phase": "entrance"},
                "fade": {"kind": "wrapper", "phase": "exit"},
            }[animation_id]

        with (
            mock.patch.object(banodoco_schema, "_animation_ids", return_value={"fade-up", "type-on", "fade"}),
            mock.patch.object(banodoco_schema, "_animation_meta", side_effect=meta),
        ):
            timeline.validate_timeline(config)

    def test_effect_params_reject_unknown_animation_reference(self) -> None:
        config = {
            "theme": "banodoco-default",
            "tracks": [],
            "clips": [
                {
                    "id": "title",
                    "at": 0,
                    "track": "v1",
                    "clipType": "text-card",
                    "hold": 2,
                    "params": {"content": "Hello", "entrance": ["missing"]},
                }
            ],
        }
        with (
            mock.patch.object(banodoco_schema, "_animation_ids", return_value={"fade-up"}),
            self.assertRaisesRegex(ValueError, "animations catalog"),
        ):
            timeline.validate_timeline(config)

    def test_effect_params_reject_animation_phase_mismatch(self) -> None:
        config = {
            "theme": "banodoco-default",
            "tracks": [],
            "clips": [
                {
                    "id": "title",
                    "at": 0,
                    "track": "v1",
                    "clipType": "text-card",
                    "hold": 2,
                    "params": {"content": "Hello", "entrance": ["fade"]},
                }
            ],
        }
        with (
            mock.patch.object(banodoco_schema, "_animation_ids", return_value={"fade"}),
            mock.patch.object(banodoco_schema, "_animation_meta", return_value={"kind": "wrapper", "phase": "exit"}),
            self.assertRaisesRegex(ValueError, "expected 'entrance'"),
        ):
            timeline.validate_timeline(config)

    def test_transition_validation_rejects_unknown_id(self) -> None:
        config = {
            "theme": "banodoco-default",
            "tracks": [],
            "clips": [
                {"id": "a", "at": 0, "track": "v1", "clipType": "media", "hold": 1, "transition": "wipe"},
                {"id": "b", "at": 1, "track": "v1", "clipType": "media", "hold": 1},
            ],
        }
        with (
            mock.patch.object(banodoco_schema, "_transition_ids", return_value={"fade"}),
            self.assertRaisesRegex(ValueError, "transitions catalog"),
        ):
            timeline.validate_timeline(config)

    def test_transition_validation_rejects_duration_that_exceeds_adjacent_clip(self) -> None:
        config = {
            "theme": "banodoco-default",
            "tracks": [],
            "clips": [
                {"id": "a", "at": 0, "track": "v1", "clipType": "media", "hold": 1, "transition": {"id": "fade", "durationFrames": 45}},
                {"id": "b", "at": 1, "track": "v1", "clipType": "media", "hold": 1},
            ],
        }
        with (
            mock.patch.object(banodoco_schema, "_transition_ids", return_value={"fade"}),
            self.assertRaisesRegex(ValueError, "fit both adjacent"),
        ):
            timeline.validate_timeline(config)

    def test_transition_validation_allows_valid_adjacent_same_track_duration(self) -> None:
        config = {
            "theme": "banodoco-default",
            "tracks": [],
            "clips": [
                {"id": "a", "at": 0, "track": "v1", "clipType": "media", "hold": 1, "transition": {"id": "fade", "durationFrames": 12}},
                {"id": "b", "at": 1, "track": "v1", "clipType": "media", "hold": 1},
                {"id": "overlay", "at": 0.2, "track": "v2", "clipType": "media", "hold": 0.2, "transition": {"id": "fade", "durationFrames": 12}},
            ],
        }
        with mock.patch.object(banodoco_schema, "_transition_ids", return_value={"fade"}):
            timeline.validate_timeline(config)


if __name__ == "__main__":
    unittest.main()
