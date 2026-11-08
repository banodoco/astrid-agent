"""The element.yaml metadata contract (X-29): role, text, summary, geometry, presets, reserved params."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from astrid.core.element import ElementValidationError, load_element_definition
from astrid.core.element.metadata import ROLE_VOCABULARY, check_element_metadata
from astrid.core.element.schema import load_element_definition
from astrid.core.timeline.clip_fields import reserved_names


def _base_definition() -> dict:
    return {
        "id": "x29-probe",
        "metadata": {"role": "card", "text": ["title", "lines[].text"], "summary": "{title}"},
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "lines": {"type": "array"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
            },
        },
        "defaults": {},
    }


def _with(**metadata_updates) -> dict:
    definition = _base_definition()
    definition["metadata"] = {**definition["metadata"], **metadata_updates}
    return definition


class CheckElementMetadataTest(unittest.TestCase):
    def test_valid_block_has_no_problems(self) -> None:
        definition = _with(geometry={"position": ["x", "y"], "scale": "width", "full_frame": False})
        self.assertEqual(check_element_metadata(definition), [])

    def test_vocabulary_and_extension_roles(self) -> None:
        self.assertIn("transition", ROLE_VOCABULARY)
        self.assertEqual(check_element_metadata(_with(role="x-hologram")), [])
        problems = check_element_metadata(_with(role="widget"))
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("x29-probe: metadata.role 'widget' is not a role"))

    def test_text_path_must_name_a_declared_param(self) -> None:
        problems = check_element_metadata(_with(text=["title", "caption[].text"]))
        self.assertEqual(
            problems,
            ["x29-probe: metadata.text path 'caption[].text' is not a declared param (schema.properties.caption)"],
        )

    def test_geometry_params_must_exist(self) -> None:
        problems = check_element_metadata(_with(geometry={"position": ["x", "top"], "scale": "size"}))
        self.assertIn("x29-probe: metadata.geometry.position param 'top' is not a declared param", problems)
        self.assertIn("x29-probe: metadata.geometry.scale param 'size' is not a declared param", problems)
        self.assertEqual(len(problems), 2)

    def test_preset_keys_must_exist(self) -> None:
        problems = check_element_metadata(_with(presets={"title": {"title": "Hi", "font": "display"}}))
        self.assertEqual(problems, ["x29-probe: metadata.presets.title param 'font' is not a declared param"])

    def test_undeclared_reserved_param_is_refused(self) -> None:
        definition = _base_definition()
        definition["schema"]["properties"]["opacity"] = {"type": "number"}
        problems = check_element_metadata(definition)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("x29-probe: param 'opacity' is a universal clip field"))

    def test_reserved_param_marked_deprecated_passes(self) -> None:
        definition = _base_definition()
        definition["schema"]["properties"]["opacity"] = {"type": "number", "deprecated": True}
        self.assertEqual(check_element_metadata(definition), [])

    def test_every_implemented_clip_field_is_refused_without_deprecated(self) -> None:
        from astrid.core.timeline.clip_fields import FIELDS

        implemented = sorted(name for name in reserved_names() if name in FIELDS)
        self.assertIn("opacity", implemented)
        for name in implemented:
            definition = _base_definition()
            definition["schema"]["properties"][name] = {"type": "number"}
            problems = check_element_metadata(definition)
            self.assertTrue(any(f"'{name}'" in p for p in problems), name)

    def test_a_name_reserved_for_later_is_not_enforced_until_its_field_exists(self) -> None:
        from astrid.core.timeline.clip_fields import FIELDS

        later = sorted(name for name in reserved_names() if name not in FIELDS)
        self.assertIn("pan", later)  # am-snap-plate's own stepped `pan` drift stays a plain param for now
        for name in later:
            definition = _base_definition()
            definition["schema"]["properties"][name] = {"type": "number"}
            self.assertEqual([p for p in check_element_metadata(definition) if f"'{name}'" in p], [], name)


class ElementLoaderHookTest(unittest.TestCase):
    def _load(self, definition: dict) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "x29-probe"
            root.mkdir()
            (root / "component.tsx").write_text("export default function E() { return null; }\n", encoding="utf-8")
            payload = {
                "schema_version": 1,
                "id": "x29-probe",
                "kind": "effect",
                "pack_id": "test",
                "dependencies": {},
                **{k: v for k, v in copy.deepcopy(definition).items() if k != "id"},
            }
            (root / "element.yaml").write_text(json.dumps(payload), encoding="utf-8")
            load_element_definition(root, kind="effects", source="test", editable=False, priority=0)

    def test_loader_raises_with_each_problem_on_its_own_line(self) -> None:
        definition = _with(role="widget", text=["missing"])
        with self.assertRaises(ElementValidationError) as ctx:
            self._load(definition)
        lines = str(ctx.exception).split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith("x29-probe: ") for line in lines))

    def test_loader_refuses_undeclared_reserved_param(self) -> None:
        definition = _base_definition()
        definition["schema"]["properties"]["opacity"] = {"type": "number"}
        with self.assertRaisesRegex(ElementValidationError, "x29-probe: param 'opacity' is a universal clip field"):
            self._load(definition)

    def test_loader_accepts_deprecated_reserved_param(self) -> None:
        definition = _base_definition()
        definition["schema"]["properties"]["volume"] = {"type": "number", "deprecated": True}
        self._load(definition)


if __name__ == "__main__":
    unittest.main()
