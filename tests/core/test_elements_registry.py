import json
import tempfile
import unittest
from pathlib import Path

from astrid.core import element as element_module
from astrid.core.element import load_default_registry


_KIND_SINGULAR = {
    "effects": "effect",
    "animations": "animation",
    "transitions": "transition",
    "widgets": "widget",
}


def write_pack_element(
    pack_root: Path,
    kind: str,
    element_id: str,
    *,
    pack_id: str,
    label: str,
    js_packages: list[str] | None = None,
) -> Path:
    if not any((pack_root / name).exists() for name in ("pack.yaml", "pack.yml", "pack.json")):
        pack_root.mkdir(parents=True, exist_ok=True)
        (pack_root / "pack.yaml").write_text(f"id: {pack_id}\nname: {pack_id}\nversion: 0.1.0\n", encoding="utf-8")
    element_root = pack_root / "elements" / kind / element_id
    element_root.mkdir(parents=True)
    (element_root / "component.tsx").write_text("export default function Element() { return null; }\n", encoding="utf-8")
    (element_root / "element.yaml").write_text(
        json.dumps(
            {
                "id": element_id,
                "kind": _KIND_SINGULAR[kind],
                "pack_id": pack_id,
                "metadata": {"label": label},
                "schema": {"type": "object"},
                "defaults": {"enabled": True},
                "dependencies": {
                    "js_packages": list(js_packages or []),
                    "python_requirements": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return element_root


class ElementRegistryTest(unittest.TestCase):
    def test_element_schema_retries_after_element_is_installed_in_same_process(self) -> None:
        from unittest import mock

        from astrid.sdk import timeline_address
        from astrid.core.element.registry import clear_default_registry_cache

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / "packs"
            element = "le2a-installed-after-miss"
            timeline_address._SCHEMAS.pop(element, None)
            timeline_address._SCHEMA_BINDINGS.pop(element, None)
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": str(packs)}):
                clear_default_registry_cache()
                try:
                    with self.assertRaises(timeline_address.ElementPackNotInstalled):
                        timeline_address.element_schema(element)
                    element_root = write_pack_element(
                        packs / "demo", "effects", element, pack_id="demo", label="Installed")
                    rendered_element_root = packs / "demo" / "rendering" / "elements" / "effects" / element
                    rendered_element_root.parent.mkdir(parents=True)
                    element_root.rename(rendered_element_root)
                    element_root = rendered_element_root
                    (packs / "demo" / "pack.yaml").write_text(json.dumps({
                        "schema_version": 3, "pack_api": "2.0", "id": "demo", "name": "Demo",
                        "version": "1.0.0", "status": "active",
                        "documentation": {"kind": "none", "reason": "Test fixture."},
                        "rendering": {f"effects/{element}": {
                            "type": "element",
                            "path": f"rendering/elements/effects/{element}/element.yaml",
                            "resources": [{"path": f"rendering/elements/effects/{element}/component.tsx",
                                           "kind": "implementation"}],
                        }},
                    }))
                    schema = timeline_address.element_schema(element)
                finally:
                    timeline_address._SCHEMAS.pop(element, None)
                    timeline_address._SCHEMA_BINDINGS.pop(element, None)
                    timeline_address._SCHEMA_PROVIDER_HINTS.pop(element, None)
                    clear_default_registry_cache()

            self.assertEqual(schema["path"], str(element_root / "element.yaml"))

    def test_element_public_surface_exports_dynamic_kind_registry(self) -> None:
        self.assertIs(element_module.ElementKindRegistry, type(element_module.ELEMENT_KIND_REGISTRY))
        self.assertTrue(hasattr(element_module, "ElementKindDescriptor"))
        self.assertTrue(hasattr(element_module, "load_source_elements"))

    def test_singular_aliases_normalize_to_builtin_kind_keys(self) -> None:
        registry = load_default_registry()

        effect = registry.get("effect", "text-card")
        animation = registry.get("animation", "fade")
        transitions = registry.list("transition")

        self.assertEqual(effect.kind, "effects")
        self.assertEqual(animation.kind, "animations")
        self.assertIn("cross-fade", {item.id for item in transitions})

    def test_fade_animation_and_fade_transition_coexist_under_kind_keys(self) -> None:
        registry = load_default_registry()
        animation_fade = registry.get("animations", "fade")
        transition_fade = registry.get("transitions", "fade")
        self.assertEqual(animation_fade.kind, "animations")
        self.assertEqual(transition_fade.kind, "transitions")
        self.assertNotEqual(animation_fade.root, transition_fade.root)
        self.assertTrue(str(animation_fade.root).endswith("astrid/packs/rendering/rendering/elements/animations/fade"))
        self.assertTrue(str(transition_fade.root).endswith("astrid/packs/rendering/rendering/elements/transitions/fade"))

    def test_rendering_pack_defaults_are_discovered_with_pack_source(self) -> None:
        from unittest import mock

        from astrid.core.element import registry as registry_module
        from astrid.core.pack import discover_packs as real_discover_packs

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            with mock.patch.object(
                registry_module,
                "discover_packs",
                side_effect=lambda root=None: tuple(p for p in real_discover_packs() if p.id != "local"),
            ):
                registry = load_default_registry(project_root=project)

            by_key = registry.as_mapping()

            self.assertIn(("effects", "text-card"), by_key)
            self.assertIn(("animations", "fade"), by_key)
            self.assertIn(("transitions", "cross-fade"), by_key)
            text_card = registry.get("effects", "text-card")
            self.assertEqual(text_card.source, "pack:rendering")
            self.assertFalse(text_card.editable)
            self.assertEqual(text_card.metadata["label"], "Text Card")
            self.assertEqual(text_card.metadata["pack_id"], "rendering")


if __name__ == "__main__":
    unittest.main()
