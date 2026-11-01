import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from astrid.core import timeline
from astrid.core.element import catalog as effects_catalog

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
GENERATOR = ROOT / "scripts" / "gen_effect_registry.py"

_GENERATOR_SPEC = importlib.util.spec_from_file_location("gen_effect_registry_for_elements", GENERATOR)
assert _GENERATOR_SPEC is not None
gen_effect_registry = importlib.util.module_from_spec(_GENERATOR_SPEC)
assert _GENERATOR_SPEC.loader is not None
sys.modules[_GENERATOR_SPEC.name] = gen_effect_registry
_GENERATOR_SPEC.loader.exec_module(gen_effect_registry)


def _timeline_composition_source() -> Path:
    candidates = (
        WORKSPACE / "packages" / "timeline-composition" / "typescript" / "src" / "TimelineComposition.tsx",
        ROOT / "remotion" / "node_modules" / "@banodoco" / "timeline-composition" / "typescript" / "src" / "TimelineComposition.tsx",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return ROOT / "remotion" / "src" / "Root.tsx"


class CompositionElementTest(unittest.TestCase):
    def _assert_animation_plugin(self, animation_id: str, kind: str) -> None:
        root = ROOT / "astrid" / "packs" / "rendering" / "rendering" / "elements" / "animations" / animation_id
        for filename in ("element.yaml",):
            self.assertTrue((root / filename).is_file(), f"{root / filename} missing")
        self.assertFalse((root / "component.tsx").exists())
        self.assertEqual(effects_catalog.read_animation_meta(animation_id)["kind"], kind)
        self.assertIn("durationFrames", effects_catalog.read_animation_defaults(animation_id))

    def test_fade_animation_plugin_contract(self) -> None:
        self._assert_animation_plugin("fade", "wrapper")

    def test_fade_up_animation_plugin_contract(self) -> None:
        self._assert_animation_plugin("fade-up", "wrapper")

    def test_scale_in_animation_plugin_contract(self) -> None:
        self._assert_animation_plugin("scale-in", "wrapper")

    def test_slide_left_animation_plugin_contract(self) -> None:
        self._assert_animation_plugin("slide-left", "wrapper")

    def test_slide_up_animation_plugin_contract(self) -> None:
        self._assert_animation_plugin("slide-up", "wrapper")

    def test_type_on_animation_plugin_contract(self) -> None:
        self._assert_animation_plugin("type-on", "hook")

    def test_workspace_animation_plugins_have_runtime_contract_files(self) -> None:
        expected = {
            "fade": "wrapper",
            "fade-up": "wrapper",
            "scale-in": "wrapper",
            "slide-left": "wrapper",
            "slide-up": "wrapper",
            "type-on": "hook",
        }
        self.assertEqual(set(expected), set(effects_catalog.list_animation_ids()))
        for animation_id, kind in expected.items():
            root = ROOT / "astrid" / "packs" / "rendering" / "rendering" / "elements" / "animations" / animation_id
            for filename in ("element.yaml",):
                self.assertTrue((root / filename).is_file(), f"{root / filename} missing")
            self.assertFalse((root / "component.tsx").exists())
            self.assertEqual(effects_catalog.read_animation_meta(animation_id)["kind"], kind)

    def test_transition_plugin_is_discoverable_and_validated(self) -> None:
        self.assertIn("cross-fade", effects_catalog.list_transition_ids())
        config = {
            "theme": "banodoco-default",
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {"id": "a", "at": 0, "track": "v1", "clipType": "text-card", "hold": 1, "params": {"content": "A"}, "transition": {"id": "cross-fade", "durationFrames": 8}},
                {"id": "b", "at": 1, "track": "v1", "clipType": "text-card", "hold": 1, "params": {"content": "B"}},
            ],
        }
        timeline.validate_timeline(config)
        config["clips"][0]["transition"] = {"id": "missing-transition", "durationFrames": 8}
        with self.assertRaisesRegex(ValueError, "transitions catalog"):
            timeline.validate_timeline(config)

    def test_bundled_element_registries_generate_together(self) -> None:
        public_ids = {
            kind: frozenset(
                element.id
                for element in gen_effect_registry._element_registry().list(kind=kind)
                if element.source == "pack:rendering" and not element.component.is_file()
            )
            for kind in ("effects", "animations", "transitions")
        }
        with mock.patch.object(gen_effect_registry, "_public_registry_ids", return_value=public_ids):
            effects = gen_effect_registry.generate_element_registry("effects", include_local=False)
            animations = gen_effect_registry.generate_element_registry("animations", include_local=False)
            transitions = gen_effect_registry.generate_element_registry("transitions", include_local=False)

        self.assertIn(
            "'text-card': selectOwnedEffect('text-card', TextCard, {'rendering': TextCard}),",
            effects,
        )
        self.assertNotIn("@pack-rendering-elements-effects/text-card/component", effects)
        self.assertNotIn("@pack-rendering-elements-animations/fade-up/component", animations)
        self.assertNotIn("@pack-rendering-elements-transitions/cross-fade/component", transitions)
        self.assertIn("'audio-reactive-colour'", effects)

    def test_hype_composition_preserves_absolute_sequence_path_with_transition_series(self) -> None:
        source_path = _timeline_composition_source()
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("TimelineCompositionProps", source)
        if source_path.name == "TimelineComposition.tsx":
            # Packaged layout: the duration helper lives beside the
            # composition and is re-exported from the package index; the
            # component is exported by name (no default export).
            pkg_src = source_path.parent
            surface = [source]
            for sibling in (pkg_src / "index.ts", pkg_src / "lib" / "duration.ts"):
                if sibling.is_file():
                    surface.append(sibling.read_text(encoding="utf-8"))
            self.assertIn("getTimelineDurationInFrames", "\n".join(surface))
            declaration = re.search(
                r"(?m)^\s*export\s+(?:default\s+)?(?:const|function)\s+"
                r"TimelineComposition\b|^\s*(?:const|function)\s+"
                r"TimelineComposition\b",
                source,
            )
            self.assertIsNotNone(
                declaration,
                "TimelineComposition must have a const/function declaration",
            )
            if not declaration.group(0).lstrip().startswith("export"):
                self.assertRegex(
                    source,
                    r"(?m)^\s*export\s+default\s+TimelineComposition\b",
                )
        else:
            # In-tree shell: both symbols are imported from the package.
            self.assertIn("getTimelineDurationInFrames", source)


if __name__ == "__main__":
    unittest.main()
