import json
import os
import pytest
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from astrid.core import element as element_module
from astrid.core.element import load_default_registry
from astrid.core.element import registry as element_registry


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


def finalize_v3_pack(pack_root: Path, elements: list[tuple[str, str]]) -> None:
    """Convert helper-created element folders into a valid V3 rendering pack."""
    rendering = {}
    for kind, element_id in elements:
        source = pack_root / "elements" / kind / element_id
        target = pack_root / "rendering" / "elements" / kind / element_id
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        key = f"{kind}/{element_id}"
        rendering[key] = {
            "type": "element",
            "path": f"rendering/elements/{kind}/{element_id}/element.yaml",
            "resources": [{
                "path": f"rendering/elements/{kind}/{element_id}/component.tsx",
                "kind": "implementation",
            }],
        }
    (pack_root / "pack.yaml").write_text(json.dumps({
        "schema_version": 3,
        "pack_api": "2.0",
        "id": pack_root.name.removesuffix(".new"),
        "name": pack_root.name,
        "version": "1.0.0",
        "rendering": rendering,
    }), encoding="utf-8")


class ElementRegistryTest(unittest.TestCase):
    def test_alias_cache_tracks_exact_id_addition_and_provider_removal(self) -> None:
        """An exact effect id entering/leaving the inventory supersedes an alias cache."""
        from unittest import mock

        from astrid.core.element import catalog
        from astrid.core.element.registry import clear_default_registry_cache
        from astrid.sdk import timeline_address

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / "packs"
            alias_id = "alias-provider"
            exact_id = "shared-clip-name"
            first = write_pack_element(
                packs / "first", "effects", alias_id, pack_id="first", label="Alias",
            )
            (first / "element.yaml").write_text(json.dumps({
                "id": alias_id, "kind": "effect", "pack_id": "first",
                "metadata": {"label": "Alias", "clipTypeAliases": [exact_id]},
                "schema": {"type": "object", "properties": {"from_alias": {"type": "boolean"}}},
                "defaults": {"from_alias": True},
            }), encoding="utf-8")
            finalize_v3_pack(packs / "first", [("effects", alias_id)])
            timeline_address._SCHEMAS.pop(exact_id, None)
            timeline_address._SCHEMA_BINDINGS.pop(exact_id, None)
            timeline_address._SCHEMA_PROVIDER_HINTS.pop(exact_id, None)
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": str(packs)}):
                clear_default_registry_cache()
                try:
                    alias_schema = timeline_address.element_schema(exact_id)
                    alias_catalog = {row["id"]: row for row in catalog.list_element_descriptors("effect")
                                    if row["id"] in {alias_id, exact_id}}
                    second = write_pack_element(
                        packs / "second", "effects", exact_id, pack_id="second", label="Exact",
                    )
                    (second / "element.yaml").write_text(json.dumps({
                        "id": exact_id, "kind": "effect", "pack_id": "second",
                        "metadata": {"label": "Exact"},
                        "schema": {"type": "object", "properties": {"from_exact": {"type": "string"}}},
                        "defaults": {"from_exact": "yes"},
                    }), encoding="utf-8")
                    finalize_v3_pack(packs / "second", [("effects", exact_id)])
                    exact_schema = timeline_address.element_schema(exact_id)
                    exact_catalog = {row["id"]: row for row in catalog.list_element_descriptors("effect")
                                    if row["id"] in {alias_id, exact_id}}
                    shutil.rmtree(packs / "second")
                    removed_schema = timeline_address.element_schema(exact_id)
                    removed_catalog = {row["id"]: row for row in catalog.list_element_descriptors("effect")
                                       if row["id"] in {alias_id, exact_id}}
                finally:
                    timeline_address._SCHEMAS.pop(exact_id, None)
                    timeline_address._SCHEMA_BINDINGS.pop(exact_id, None)
                    timeline_address._SCHEMA_PROVIDER_HINTS.pop(exact_id, None)
                    catalog._clear_registry_cache()

        self.assertEqual(alias_schema["properties"], {"from_alias": {"type": "boolean"}})
        self.assertEqual(exact_catalog[alias_id]["schema"]["properties"], {"from_alias": {"type": "boolean"}})
        self.assertEqual(set(removed_catalog), {alias_id})
        self.assertEqual(removed_catalog[alias_id]["schema"]["properties"], {"from_alias": {"type": "boolean"}})

    def test_provider_exchange_between_selection_and_binding_retries_before_return(self) -> None:
        from unittest import mock

        from astrid.core.element.registry import clear_default_registry_cache
        from astrid.sdk import timeline_address

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / "packs"
            packs.mkdir()
            element = "exchange-element"
            old_root = packs / "demo"
            old_element_root = write_pack_element(old_root, "effects", element, pack_id="demo", label="Old")
            (old_element_root / "element.yaml").write_text(json.dumps({
                "id": element, "kind": "effect", "pack_id": "demo",
                "schema": {"type": "object", "properties": {"old": {"type": "boolean"}}},
                "defaults": {},
            }), encoding="utf-8")
            finalize_v3_pack(old_root, [("effects", element)])
            staged_root = Path(tmp) / "demo.new"
            new_element_root = write_pack_element(staged_root, "effects", element, pack_id="demo", label="New")
            (new_element_root / "element.yaml").write_text(json.dumps({
                "id": element, "kind": "effect", "pack_id": "demo",
                "schema": {"type": "object", "properties": {"new": {"type": "string"}}},
                "defaults": {},
            }), encoding="utf-8")
            finalize_v3_pack(staged_root, [("effects", element)])
            timeline_address._SCHEMAS.pop(element, None)
            timeline_address._SCHEMA_BINDINGS.pop(element, None)
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": str(packs)}):
                clear_default_registry_cache()
                real_binding = timeline_address._schema_binding
                selected = threading.Event()
                exchanged = threading.Event()
                outcome = {}

                def exchange_after_selection(definition, manifest):
                    selected.set()
                    if not exchanged.wait(5):
                        raise TimeoutError("pack exchange did not complete")
                    return real_binding(definition, manifest)

                try:
                    with mock.patch.object(timeline_address, "_schema_binding", side_effect=exchange_after_selection):
                        worker = threading.Thread(
                            target=lambda: outcome.setdefault("schema", timeline_address.element_schema(element)),
                            daemon=True,
                        )
                        worker.start()
                        self.assertTrue(selected.wait(5), "schema lookup did not reach binding barrier")
                        # The reader is held at the barrier until both renames finish.
                        old_root.rename(Path(tmp) / "demo.old")
                        staged_root.rename(old_root)
                        exchanged.set()
                        worker.join(5)
                        self.assertFalse(worker.is_alive(), "schema lookup remained blocked after pack exchange")
                        schema = outcome["schema"]
                finally:
                    timeline_address._SCHEMAS.pop(element, None)
                    timeline_address._SCHEMA_BINDINGS.pop(element, None)
                    timeline_address._SCHEMA_PROVIDER_HINTS.pop(element, None)
                    clear_default_registry_cache()

        self.assertTrue(exchanged.is_set())
        self.assertIn(schema["properties"], (
            {"old": {"type": "boolean"}},
            {"new": {"type": "string"}},
        ))

    def test_stable_tree_reuses_single_pack_discovery(self) -> None:
        from unittest import mock

        from astrid.core.element import registry as registry_module
        from astrid.core.element.registry import clear_default_registry_cache

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / "packs"
            write_pack_element(packs / "demo", "effects", "stable-element", pack_id="demo", label="Stable")
            finalize_v3_pack(packs / "demo", [("effects", "stable-element")])
            real_discover = registry_module.discover_packs
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": str(packs)}):
                clear_default_registry_cache()
                try:
                    with mock.patch.object(registry_module, "discover_packs", wraps=real_discover) as discover:
                        registry_module.load_default_registry()
                        first_load_count = discover.call_count
                        registry_module.load_default_registry()
                    self.assertGreater(first_load_count, 0)
                    self.assertEqual(discover.call_count, first_load_count)
                finally:
                    clear_default_registry_cache()

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

    def test_shared_miss_rediscovers_for_schema_and_reader_in_same_process(self) -> None:
        """A cached miss must not hide a newly installed pack from either reader."""
        from unittest import mock

        from astrid.core.element import catalog, registry as registry_module
        from astrid.core.element.registry import clear_default_registry_cache
        from astrid.sdk import timeline_address

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / "packs"
            element = "same-process-reader-probe"
            timeline_address._SCHEMAS.pop(element, None)
            timeline_address._SCHEMA_BINDINGS.pop(element, None)
            timeline_address._SCHEMA_PROVIDER_HINTS.pop(element, None)
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": str(packs)}):
                clear_default_registry_cache()
                try:
                    # Both consumers observe the initial absence; a stable miss clears shared discovery once.
                    self.assertNotIn(element, catalog.list_effect_ids())
                    with mock.patch.object(registry_module, "clear_default_registry_cache",
                                           wraps=registry_module.clear_default_registry_cache) as clear:
                        with self.assertRaises(timeline_address.ElementPackNotInstalled):
                            timeline_address.element_schema(element)
                        miss_clear_count = clear.call_count

                    write_pack_element(
                        packs / "demo", "effects", element, pack_id="demo", label="Installed")
                    finalize_v3_pack(packs / "demo", [("effects", element)])
                    real_discover = registry_module.discover_packs
                    with mock.patch.object(registry_module, "discover_packs", wraps=real_discover) as discover:
                        # A reader/schema path resolves the new declaration without a process restart.
                        from astrid.sdk.timeline.commit import _check_readers, create_runtime_commit_service
                        from tests.sdk.test_timeline_reader_roundtrip import _document_of, readable_payload

                        baseline_payload = readable_payload()
                        candidate_payload = readable_payload()
                        candidate_payload["clips"][0].update(clipType=element, params={"enabled": True})
                        baseline = _document_of(baseline_payload)
                        candidate = _document_of(candidate_payload)
                        base = {"revision_id": "r0", "publication_id": "pub0"}
                        _check_readers(create_runtime_commit_service(), candidate, baseline, base, base)
                        schema = timeline_address.element_schema(element)
                        registry = load_default_registry()
                        self.assertIs(type(registry), registry_module.ElementRegistry)
                        self.assertEqual(registry.get("effects", element).schema["type"], "object")
                        fresh_process = json.loads(subprocess.check_output(
                            [sys.executable, "-c", (
                                "import json; from astrid.sdk.timeline_address import element_schema; "
                                "print(json.dumps(element_schema('same-process-reader-probe')['properties']))"
                            )],
                            text=True,
                        ))
                        self.assertEqual(schema["path"], str(
                            packs / "demo" / "rendering" / "elements" / "effects" / element / "element.yaml"))
                        self.assertEqual(schema["properties"], fresh_process)
                        self.assertIn(element, catalog.list_effect_ids())
                        # The registry corpus is shared; the follow-up hit is the fast path.
                        discovery_count = discover.call_count
                        self.assertGreater(discovery_count, 0)
                        self.assertEqual(timeline_address.element_schema(element), schema)
                        self.assertEqual(discover.call_count, discovery_count)
                    self.assertEqual(miss_clear_count, 1)
                finally:
                    timeline_address._SCHEMAS.pop(element, None)
                    timeline_address._SCHEMA_BINDINGS.pop(element, None)
                    timeline_address._SCHEMA_PROVIDER_HINTS.pop(element, None)
                    clear_default_registry_cache()

    def test_schema_cache_hit_does_not_resample_collection_state(self) -> None:
        """A valid cached schema is returned without traversing pack-root state."""
        from unittest import mock

        from astrid.core.element import registry as registry_module
        from astrid.core.element.registry import clear_default_registry_cache
        from astrid.sdk import timeline_address

        with tempfile.TemporaryDirectory() as tmp:
            packs = Path(tmp) / "packs"
            element = "cached-schema-fast-path"
            write_pack_element(
                packs / "demo", "effects", element, pack_id="demo", label="Cached")
            finalize_v3_pack(packs / "demo", [("effects", element)])
            rendered_root = packs / "demo" / "rendering" / "elements" / "effects" / element
            timeline_address._SCHEMAS.pop(element, None)
            timeline_address._SCHEMA_BINDINGS.pop(element, None)
            timeline_address._SCHEMA_PROVIDER_HINTS.pop(element, None)
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": str(packs)}):
                clear_default_registry_cache()
                try:
                    schema = timeline_address.element_schema(element)
                    self.assertEqual(schema["path"], str(rendered_root / "element.yaml"))
                    with mock.patch.object(
                        registry_module,
                        "discover_packs",
                        side_effect=AssertionError("cached schema hit rescanned pack discovery"),
                    ):
                        self.assertIs(timeline_address.element_schema(element), schema)
                finally:
                    timeline_address._SCHEMAS.pop(element, None)
                    timeline_address._SCHEMA_BINDINGS.pop(element, None)
                    timeline_address._SCHEMA_PROVIDER_HINTS.pop(element, None)
                    clear_default_registry_cache()

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


def test_schema_warm_reader_survives_concurrent_miss_clear(monkeypatch):
    from astrid.sdk import timeline_address

    entered = threading.Event()
    resume = threading.Event()
    schema = {"properties": {"warm": {"type": "string"}}}
    timeline_address.clear_schema_cache()
    timeline_address._SCHEMAS["warm-race"] = schema
    timeline_address._SCHEMA_BINDINGS["warm-race"] = ("pack", Path("."), Path("."), (), ())

    def current(_element):
        entered.set()
        assert resume.wait(30)
        return True

    monkeypatch.setattr(timeline_address, "_cached_schema_is_current", current)
    outcome = {}

    def warm_reader():
        try:
            outcome["schema"] = timeline_address.element_schema("warm-race")
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=warm_reader)
    worker.start()
    try:
        assert entered.wait(30)
        with unittest.TestCase().assertRaises(timeline_address.ElementSchemaUnavailable):
            timeline_address.element_schema("missing-race-element")
    finally:
        resume.set()
        worker.join(30)
        timeline_address.clear_schema_cache()
    assert not worker.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    assert outcome["schema"] is schema


@pytest.mark.parametrize("retry", [False, True], ids=["initial", "retry"])
@pytest.mark.parametrize("explicit_root", [False, True], ids=["default-root", "explicit-root"])
def test_lookup_default_registry_omits_default_root_keyword(monkeypatch, retry, explicit_root):
    project_root = Path("/tmp/explicit-project")
    calls = []
    registries = [object(), object()]

    def load_default_registry(**kwargs):
        calls.append(kwargs)
        return registries[len(calls) - 1]

    lookup_results = iter([None, "found"] if retry else ["found"])

    monkeypatch.setattr(element_registry, "load_default_registry", load_default_registry)
    monkeypatch.setattr(element_registry, "clear_default_registry_cache", lambda: None)

    kwargs = {"project_root": project_root} if explicit_root else {}
    result, registry = element_registry.lookup_default_registry(
        lambda _registry: next(lookup_results),
        **kwargs,
    )

    assert result == "found"
    assert registry is registries[1 if retry else 0]
    expected_call = {"project_root": project_root} if explicit_root else {}
    assert calls == [expected_call] * (2 if retry else 1)
