import unittest
from pathlib import Path

from astrid.core.element.schema import load_element_definition

ROOT = Path(__file__).resolve().parents[2]
PLATE = ROOT / "astrid" / "packs" / "astrid_motion" / "rendering" / "elements" / "effects" / "am-snap-plate"


class AmSnapPlateFillTest(unittest.TestCase):
    def _definition(self):
        return load_element_definition(PLATE, kind="effects", source="pack:astrid_motion", editable=False, priority=30)

    def test_fill_is_declared_in_schema_and_not_a_default(self) -> None:
        definition = self._definition()
        self.assertIn("fill", definition.schema.get("properties", {}))
        # Existing clips keep their rendering: fill is opt-in and has no default.
        self.assertNotIn("fill", definition.defaults)
        self.assertEqual(definition.defaults["background"], "#F7F4ED")

    def test_component_renders_without_an_asset_when_fill_is_set(self) -> None:
        source = (PLATE / "component.tsx").read_text(encoding="utf-8")
        # Returns null only when there is neither a source nor a fill.
        self.assertIn("if (!url && !fill) {", source)
        self.assertIn("FILL_TOKENS", source)
        # The image is only drawn when there is a url.
        self.assertIn("{!url ? null : block > 0 ? (", source)
        self.assertIn("params.background ?? fill ?? COLOR.paper", source)

    def test_skill_does_not_ask_for_placeholder_pngs(self) -> None:
        text = (ROOT / "astrid" / "packs" / "astrid_motion" / "docs" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("params.fill", text)


if __name__ == "__main__":
    unittest.main()
