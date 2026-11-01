import json
import shutil
import subprocess
import unittest
from pathlib import Path

import pytest

from astrid.core.element.schema import load_element_definition

ROOT = Path(__file__).resolve().parents[3]
ELEMENT = ROOT / "astrid" / "packs" / "astrid_motion" / "rendering" / "elements" / "effects" / "am-pixel-shape"
CELLS = ELEMENT / "shape-cells.ts"
# The pinned Node (20.19.4) cannot run TypeScript, so esbuild (installed with the Remotion renderer) transpiles the source.
ESBUILD = ROOT / "remotion" / "node_modules" / "esbuild" / "lib" / "main.js"
LOADER = """
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require = createRequire(import.meta.url);
const esbuild = require(process.argv[1]);
const js = esbuild.transformSync(readFileSync(process.argv[2], 'utf8'), {loader: 'ts', format: 'esm'}).code;
const m = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
console.log(JSON.stringify(eval(process.argv[3])));
"""


def _call(expression: str):
    """Evaluate ``expression`` (using ``m``, the transpiled shape-cells module) in Node and return its JSON."""
    out = subprocess.run(
        [shutil.which("node") or "node", "--input-type=module", "-e", LOADER, str(ESBUILD), str(CELLS), expression],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def _cells(shape: str, w: int, h: int, stroke: int, radius: int = 0) -> list[list[int]]:
    return _call(f"m.shapeCells({json.dumps(shape)}, {w}, {h}, {stroke}, {radius})")


@pytest.mark.remotion
class AmPixelShapeGeometryTest(unittest.TestCase):
    def test_dot_three_by_three_is_a_full_square(self) -> None:
        cells = _cells("dot", 3, 3, 1)
        self.assertEqual(len(cells), 9)

    def test_ring_is_hollow_and_inside_the_box(self) -> None:
        cells = _cells("ring", 12, 12, 2)
        self.assertTrue(cells)
        self.assertNotIn([5, 5], cells)  # centre is empty
        self.assertIn([0, 6], cells)  # left edge is inked
        self.assertTrue(all(0 <= c < 12 and 0 <= r < 12 for c, r in cells))
        filled = _cells("dot", 12, 12, 2)
        self.assertLess(len(cells), len(filled))

    def test_underline_is_bottom_rows_only(self) -> None:
        cells = _cells("underline", 10, 6, 2)
        self.assertEqual(len(cells), 20)
        self.assertEqual({r for _, r in cells}, {4, 5})

    def test_arrow_points_right(self) -> None:
        cells = _cells("arrow", 12, 6, 1)
        self.assertEqual(len(cells), 30)
        self.assertIn([11, 2], cells)  # tip reaches the right edge
        self.assertNotIn([0, 0], cells)


def _rect(w: int, h: int, stroke: int, radius: int) -> dict[str, list[list[int]]]:
    return _call(f"m.rectCells({w}, {h}, {stroke}, {radius})")


@pytest.mark.remotion
class AmPixelShapeRectTest(unittest.TestCase):
    def test_rounded_tile_has_expected_cell_counts(self) -> None:
        # 16x16 r2: the silhouette loses one cell per corner (256 - 4 = 252).
        # A 1-cell outline is the 56-cell border; the interior is the other 196.
        rect = _rect(16, 16, 1, 2)
        self.assertEqual(len(rect["fill"]), 252)
        self.assertEqual(len(rect["outline"]), 56)
        outline = {tuple(c) for c in rect["outline"]}
        fill = {tuple(c) for c in rect["fill"]}
        self.assertEqual(len(fill - outline), 196)
        self.assertTrue(outline <= fill)

    def test_corners_are_stepped(self) -> None:
        rect = _rect(16, 16, 1, 2)
        fill = {tuple(c) for c in rect["fill"]}
        self.assertNotIn((0, 0), fill)  # corner cell is cut
        self.assertIn((1, 0), fill)  # the step beside it stays
        self.assertIn((0, 1), fill)
        # A larger radius cuts a bigger, still stepped, corner: (1, 0) goes, (5, 0) stays.
        wide = {tuple(c) for c in _rect(16, 16, 1, 6)["fill"]}
        self.assertNotIn((1, 0), wide)
        self.assertIn((5, 0), wide)
        self.assertNotIn((15, 15), wide)

    def test_square_rect_is_the_full_box_and_outline_is_the_border(self) -> None:
        rect = _rect(10, 6, 1, 0)
        self.assertEqual(len(rect["fill"]), 60)
        self.assertEqual(len(rect["outline"]), 2 * 10 + 2 * 4)
        self.assertNotIn([5, 3], rect["outline"])  # interior is not outlined

    def test_radius_is_clamped_and_stroke_thicker_than_half_is_safe(self) -> None:
        clamped = _rect(8, 4, 1, 99)
        self.assertTrue(all(0 <= c < 8 and 0 <= r < 4 for c, r in clamped["fill"]))
        thick = _rect(6, 6, 9, 0)
        self.assertEqual(len(thick["outline"]), len(thick["fill"]))

    def test_shape_rect_returns_the_silhouette(self) -> None:
        self.assertEqual(sorted(_cells("rect", 16, 16, 1, 2)), sorted(_rect(16, 16, 1, 2)["fill"]))


def _blinking(frame: int, blink_at) -> bool:
    return _call(f"m.isBlinking({frame}, {json.dumps(blink_at)})")


@pytest.mark.remotion
class AmPixelShapeBlinkTest(unittest.TestCase):
    def test_blink_hides_for_two_frames_at_each_listed_frame(self) -> None:
        # Same rule as am-sprite blinkAt: frames 14, 28, 42 hide the shape for two frames each.
        hidden = [f for f in range(48) if _blinking(f, [14, 28, 42])]
        self.assertEqual(hidden, [14, 15, 28, 29, 42, 43])

    def test_single_number_and_missing_blink_at(self) -> None:
        self.assertEqual([f for f in range(6) if _blinking(f, 3)], [3, 4])
        self.assertFalse(any(_blinking(f, None) for f in range(10)))


class AmPixelShapeElementTest(unittest.TestCase):
    def test_element_loads_with_sticker_grid_defaults(self) -> None:
        definition = load_element_definition(
            ELEMENT, kind="effects", source="pack:astrid_motion", editable=False, priority=30
        )
        self.assertEqual(definition.id, "am-pixel-shape")
        self.assertEqual(definition.defaults["px_scale"], 6)
        self.assertEqual(
            definition.schema["properties"]["shape"]["enum"], ["ring", "dot", "underline", "arrow", "rect"]
        )
        for key in ("fill", "outline", "radius"):
            self.assertIn(key, definition.schema["properties"])

    def test_component_uses_the_shared_logical_grid(self) -> None:
        source = (ELEMENT / "component.tsx").read_text(encoding="utf-8")
        self.assertIn("LOGICAL_PX", source)
        self.assertIn("shapeCells(shape, w, h, stroke)", source)
        self.assertIn('shapeRendering="crispEdges"', source)

    def test_blink_at_is_declared_and_used(self) -> None:
        definition = load_element_definition(
            ELEMENT, kind="effects", source="pack:astrid_motion", editable=False, priority=30
        )
        self.assertIn("blinkAt", definition.schema["properties"])
        source = (ELEMENT / "component.tsx").read_text(encoding="utf-8")
        self.assertIn("isBlinking(frame, params.blinkAt)", source)


if __name__ == "__main__":
    unittest.main()
