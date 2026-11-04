import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "scripts" / "gen_effect_registry.py"

_SPEC = importlib.util.spec_from_file_location("gen_effect_registry_elements_test", GENERATOR)
assert _SPEC is not None
gen_effect_registry = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gen_effect_registry
assert _SPEC.loader is not None
_SPEC.loader.exec_module(gen_effect_registry)


class RemotionElementGenerationTest(unittest.TestCase):
    def test_generated_registries_use_element_scope_aliases(self) -> None:
        for kind in ("effects", "animations", "transitions"):
            generated = gen_effect_registry.generate_element_registry(kind)
            self.assertIn("@banodoco/timeline-composition/theme-api", generated)
            self.assertNotIn("./effects.types", generated)
            self.assertNotIn("@workspace-", generated)
            self.assertNotIn("primitive-root", generated)
        self.assertIn("@pack-rendering-elements-effects/audio-reactive-colour/component", gen_effect_registry.generate_element_registry("effects"))

    def test_canonical_registry_winner_is_not_replaced_by_local_shadow(self) -> None:
        from astrid.core.element.registry import ElementRegistry
        from astrid.core.element.schema import ElementDefinition

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_root = root / "rendering" / "fade"
            public_root.mkdir(parents=True)
            public_component = public_root / "component.tsx"
            public_component.write_text("export default function PublicFade() { return null; }\n", encoding="utf-8")
            override_root = root / "local" / "fade"
            override_root.mkdir(parents=True)
            override_component = override_root / "component.tsx"
            override_component.write_text("export default function FadeOverride() { return null; }\n", encoding="utf-8")

            public_descriptor = ElementDefinition(
                id="fade", kind="animations", root=public_root, source="pack:rendering",
                editable=False, priority=30, component=public_component,
                schema={}, defaults={"durationFrames": 12},
                metadata={"pack_id": "rendering", "kind": "wrapper"},
                runtime={"adapter": "remotion"},
            )
            override = ElementDefinition(
                id="fade", kind="animations", root=override_root, source="pack:local",
                editable=True, priority=30, component=override_component, schema={},
                defaults={"durationFrames": 24}, metadata={"pack_id": "local"},
                runtime={"adapter": "remotion"},
            )
            old_registry = gen_effect_registry._element_registry
            gen_effect_registry._element_registry = lambda: ElementRegistry(
                (public_descriptor, override)
            )
            try:
                generated = gen_effect_registry.generate_element_registry("animations")
            finally:
                gen_effect_registry._element_registry = old_registry

        self.assertIn("'fade'", generated)
        self.assertIn("@pack-rendering-elements-animations/fade/component", generated)
        self.assertNotIn("@pack-local-elements-animations/fade/component", generated)

    def test_public_source_less_winner_preserves_local_owner(self) -> None:
        from astrid.core.element.registry import load_default_registry

        registry = load_default_registry(project_root=ROOT)
        winner = registry.get("effects", "text-card")
        self.assertEqual(winner.source, "pack:rendering")
        self.assertFalse(winner.component.is_file())
        self.assertEqual(winner.metadata["pack_id"], "rendering")

        public_ids = gen_effect_registry._public_registry_ids()
        self.assertIn("text-card", public_ids["effects"])
        generated = gen_effect_registry.generate_element_registry("effects")
        self.assertIn("'text-card': selectOwnedEffect(", generated)
        self.assertIn("PUBLIC_EFFECT_REGISTRY['text-card']", generated)
        self.assertIn("'local': OwnerElement", generated)
        self.assertIn("'rendering': PUBLIC_EFFECT_REGISTRY['text-card']", generated)
        self.assertNotIn("@pack-rendering-elements-effects/text-card/component", generated)

    def test_source_less_transition_keeps_catalog_defaults_and_precedence(self) -> None:
        from astrid.core.element.registry import ElementRegistry
        from astrid.core.element.schema import ElementDefinition

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_root = root / "rendering" / "fade"
            public_root.mkdir(parents=True)
            shadow_root = root / "local" / "fade"
            shadow_root.mkdir(parents=True)
            custom_root = root / "probe-fade"
            custom_root.mkdir(parents=True)
            component = shadow_root / "component.tsx"
            component.write_text(
                "export default () => ({presentation: null, timing: null});\n",
                encoding="utf-8",
            )
            custom_component = custom_root / "component.tsx"
            custom_component.write_text(component.read_text(encoding="utf-8"), encoding="utf-8")
            winner = ElementDefinition(
                id="fade", kind="transitions", root=public_root, source="pack:rendering",
                editable=False, priority=30, component=public_root / "retired.tsx",
                schema={}, defaults={"durationFrames": 8}, metadata={},
            )
            shadow = ElementDefinition(
                id="fade", kind="transitions", root=shadow_root, source="pack:local",
                editable=True, priority=100, component=component,
                schema={}, defaults={"durationFrames": 24}, metadata={},
            )
            custom = ElementDefinition(
                id="probe-fade", kind="transitions", root=custom_root, source="pack:probe",
                editable=True, priority=30, component=custom_component,
                schema={}, defaults={"durationFrames": 6}, metadata={},
            )
            with mock.patch.object(
                gen_effect_registry,
                "_element_registry",
                return_value=ElementRegistry((winner, shadow, custom)),
            ), mock.patch.object(
                gen_effect_registry,
                "_public_registry_ids",
                return_value={"transitions": {"fade"}},
            ):
                generated = gen_effect_registry.generate_element_registry("transitions")

        self.assertIn("'fade': {\"durationFrames\":8}", generated)
        self.assertIn("'probe-fade': {\"durationFrames\":6}", generated)
        self.assertNotIn('durationFrames\":24', generated)
        self.assertNotIn("@pack-rendering-elements-transitions/fade/component", generated)
        self.assertNotIn("@pack-local-elements-transitions/fade/component", generated)
        self.assertIn("@pack-probe-elements-transitions/probe-fade/component", generated)
        self.assertIn("TRANSITION_IDS = ['fade', 'probe-fade']", generated)

    def test_public_membership_uses_composition_registry_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            package = project / "node_modules" / "@banodoco" / "timeline-composition"
            package.mkdir(parents=True)
            (package / "package.json").write_text("{}\n", encoding="utf-8")
            response = json.dumps({
                "effects": ["package-effect"],
                "animations": ["package-animation"],
                "transitions": ["package-transition"],
            })
            old_cache = gen_effect_registry._PUBLIC_REGISTRY_CACHE
            gen_effect_registry._PUBLIC_REGISTRY_CACHE = {}
            try:
                with mock.patch.dict(
                    gen_effect_registry.os.environ,
                    {
                        "ASTRID_REMOTION_PROJECT_DIR": str(project),
                        "ASTRID_NODE_EXECUTABLE": "/node20",
                    },
                    clear=False,
                ), mock.patch.object(
                    gen_effect_registry.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["/node20"], 0, stdout=response, stderr=""
                    ),
                ) as run:
                    membership = gen_effect_registry._public_registry_ids()
            finally:
                gen_effect_registry._PUBLIC_REGISTRY_CACHE = old_cache

        self.assertEqual(membership["effects"], {"package-effect"})
        command = run.call_args.args[0]
        self.assertIn("resolveElementRegistries", command[-1])
        self.assertNotIn("text-card", command[-1])

    def test_missing_non_public_component_is_diagnostic(self) -> None:
        from astrid.core.element.registry import ElementRegistry
        from astrid.core.element.schema import ElementDefinition

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom" / "missing"
            root.mkdir(parents=True)
            missing = ElementDefinition(
                id="missing", kind="effects", root=root, source="pack:custom",
                editable=True, priority=0, component=root / "component.tsx",
                schema={}, defaults={}, metadata={"pack_id": "custom"},
                runtime={"adapter": "remotion"},
            )
            old_registry = gen_effect_registry._element_registry
            gen_effect_registry._element_registry = lambda: ElementRegistry((missing,))
            try:
                with self.assertRaisesRegex(ValueError, "broken custom component"):
                    gen_effect_registry.generate_element_registry("effects")
            finally:
                gen_effect_registry._element_registry = old_registry

    def test_unknown_rendering_component_is_not_silently_omitted(self) -> None:
        from astrid.core.element.registry import ElementRegistry
        from astrid.core.element.schema import ElementDefinition

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rendering" / "unknown"
            root.mkdir(parents=True)
            missing = ElementDefinition(
                id="unknown", kind="effects", root=root, source="pack:rendering",
                editable=False, priority=30, component=root / "component.tsx",
                schema={}, defaults={}, metadata={"pack_id": "rendering"},
                runtime={"adapter": "remotion"},
            )
            old_registry = gen_effect_registry._element_registry
            old_public_ids = gen_effect_registry._public_registry_ids
            gen_effect_registry._element_registry = lambda: ElementRegistry((missing,))
            gen_effect_registry._public_registry_ids = lambda: {
                "effects": frozenset(), "animations": frozenset(), "transitions": frozenset(),
            }
            try:
                with self.assertRaisesRegex(ValueError, "broken rendering component"):
                    gen_effect_registry.generate_element_registry("effects")
            finally:
                gen_effect_registry._element_registry = old_registry
                gen_effect_registry._public_registry_ids = old_public_ids

    def test_remotion_alias_files_do_not_reference_workspace_element_aliases(self) -> None:
        checked = [
            ROOT / "scripts" / "gen_effect_registry.py",
            ROOT / "remotion" / "tsconfig.json",
        ]
        for path in checked:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("@workspace-", text)
            self.assertNotIn("workspace-effects", text)
            self.assertNotIn("workspace-animations", text)
            self.assertNotIn("workspace-transitions", text)

    def test_generated_effect_registry_hashes_manifest_component_support_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            project = workspace / "project"
            local_pack = project / "astrid" / "packs" / "local"
            element_root = local_pack / "elements" / "effects" / "fingerprint-probe"
            assets_root = element_root / "assets"
            assets_root.mkdir(parents=True)
            (local_pack / "pack.yaml").write_text(
                "schema_version: 2\nid: local\nname: Local\nversion: 0.1.0\n"
                "capabilities: [elements]\n",
                encoding="utf-8",
            )
            component = element_root / "component.tsx"
            support_file = element_root / "support.css"
            asset = assets_root / "badge.txt"
            manifest = element_root / "element.yaml"

            component.write_text(
                "export default function FingerprintProbe() { return null; }\n",
                encoding="utf-8",
            )
            support_file.write_text(".fingerprint-probe { color: red; }\n", encoding="utf-8")
            asset.write_text("badge-v1\n", encoding="utf-8")
            manifest.write_text(
                json.dumps(
                    {
                        "id": "fingerprint-probe",
                        "kind": "effect",
                        "pack_id": "local",
                        "metadata": {"label": "Fingerprint Probe"},
                        "schema": {"type": "object"},
                        "defaults": {},
                        "assets": {"badge": "assets/badge.txt"},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            from astrid.core.element import registry as element_registry
            from astrid.core.pack import discover_packs as real_discover_packs

            old_tools_dir = gen_effect_registry.TOOLS_DIR
            old_discover = element_registry.discover_packs

            def generated_fingerprint_and_import() -> tuple[str, str]:
                generated = gen_effect_registry.generate_element_registry("effects")
                fingerprint_match = re.search(
                    r"'fingerprint-probe': '([0-9a-f]{64})'",
                    generated,
                )
                self.assertIsNotNone(fingerprint_match)
                fingerprint = fingerprint_match.group(1)
                import_match = re.search(
                    r"import FingerprintProbe from '([^']+)';",
                    generated,
                )
                self.assertIsNotNone(import_match)
                import_path = import_match.group(1)
                self.assertEqual(
                    import_path,
                    "@pack-local-elements-effects/fingerprint-probe/component"
                    f"?astrid={fingerprint[:12]}",
                )
                self.assertIn(
                    "export const EFFECT_FINGERPRINTS: Record<EffectId, string> = {",
                    generated,
                )
                return fingerprint, import_path

            try:
                gen_effect_registry.TOOLS_DIR = project
                element_registry.discover_packs = (
                    lambda root=None: real_discover_packs(local_pack.parent)
                )

                original = generated_fingerprint_and_import()
                self.assertEqual(original, generated_fingerprint_and_import())

                component.write_text(
                    "export default function FingerprintProbe() { return 'component-v2'; }\n",
                    encoding="utf-8",
                )
                after_component = generated_fingerprint_and_import()
                self.assertNotEqual(original, after_component)

                component.write_text(
                    "export default function FingerprintProbe() { return null; }\n",
                    encoding="utf-8",
                )
                self.assertEqual(original, generated_fingerprint_and_import())

                support_file.write_text(
                    ".fingerprint-probe { color: blue; }\n",
                    encoding="utf-8",
                )
                after_support = generated_fingerprint_and_import()
                self.assertNotEqual(original, after_support)

                support_file.write_text(
                    ".fingerprint-probe { color: red; }\n",
                    encoding="utf-8",
                )
                self.assertEqual(original, generated_fingerprint_and_import())

                asset.write_text("badge-v2\n", encoding="utf-8")
                after_asset = generated_fingerprint_and_import()
                self.assertNotEqual(original, after_asset)

                asset.write_text("badge-v1\n", encoding="utf-8")
                self.assertEqual(original, generated_fingerprint_and_import())

                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["metadata"]["label"] = "Fingerprint Probe v2"
                manifest.write_text(
                    json.dumps(payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                after_manifest = generated_fingerprint_and_import()
                self.assertNotEqual(original, after_manifest)
            finally:
                gen_effect_registry.TOOLS_DIR = old_tools_dir
                element_registry.discover_packs = old_discover


if __name__ == "__main__":
    unittest.main()
