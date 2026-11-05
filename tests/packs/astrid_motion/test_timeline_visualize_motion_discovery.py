"""Cheap, render-free proof for admitted V3 motion-resource discovery."""

from __future__ import annotations

import json
from types import SimpleNamespace

from astrid.core.element.registry import load_default_registry
from astrid.packs.rendering.actions.timeline_visualize.motion import element_motion


def _declaration(tmp_path, pack_id: str, element_id: str) -> dict:
    pack_root = tmp_path / pack_id
    element_root = pack_root / "rendering" / "elements" / "effects" / element_id
    element_root.mkdir(parents=True)
    (element_root / "element.yaml").write_text(
        f"schema_version: 1\nid: {element_id}\nkind: effect\npack_id: {pack_id}\n"
        "schema: {type: object}\n",
        encoding="utf-8",
    )
    (element_root / "motion.ts").write_text(
        "export function motionAt(params, clipFrame, fps) { return {x: clipFrame, y: fps}; }\n",
        encoding="utf-8",
    )
    return {
        "pack_id": pack_id,
        "kind": "effects",
        "id": element_id,
        "pack_root": str(pack_root),
        "manifest_path": str(element_root / "element.yaml"),
        "resources": [{
            "kind": "implementation",
            "path": f"rendering/elements/effects/{element_id}/motion.ts",
        }],
    }


def _installed_motion_pack(root):
    pack_id = "motion_fixture"
    declarations = {}
    for kind, element_id in (
        ("animations", "installed-animation"),
        ("transitions", "installed-transition"),
    ):
        singular = kind[:-1]
        relative = f"rendering/elements/{kind}/{element_id}"
        element_root = root / relative
        element_root.mkdir(parents=True)
        (element_root / "element.yaml").write_text(
            json.dumps({
                "schema_version": 1,
                "id": element_id,
                "kind": singular,
                "pack_id": pack_id,
                "schema": {"type": "object"},
            }),
            encoding="utf-8",
        )
        (element_root / "component.tsx").write_text(
            "export default function Element() { return null; }\n", encoding="utf-8"
        )
        (element_root / "motion.ts").write_text(
            "export function motionAt(params, clipFrame, fps) { return {x: clipFrame}; }\n",
            encoding="utf-8",
        )
        declarations[f"{kind}/{element_id}"] = {
            "type": "element",
            "path": f"{relative}/element.yaml",
            "resources": [
                {"kind": "implementation", "path": f"{relative}/component.tsx"},
                {"kind": "implementation", "path": f"{relative}/motion.ts"},
            ],
        }
    (root / "pack.yaml").write_text(
        "schema_version: 3\n"
        f"id: {pack_id}\nname: Motion Fixture\nversion: 1.0.0\nstatus: active\n"
        + "rendering:\n"
        + "".join(
            f"  {key}:\n"
            f"    type: element\n"
            f"    path: {value['path']}\n"
            "    resources:\n"
            + "".join(
                f"      - kind: {resource['kind']}\n        path: {resource['path']}\n"
                for resource in value["resources"]
            )
            for key, value in declarations.items()
        ),
        encoding="utf-8",
    )
    return pack_id


def test_duplicate_element_basenames_require_canonical_pack_identity(tmp_path):
    first = _declaration(tmp_path, "pack_one", "shared")
    second = _declaration(tmp_path, "pack_two", "shared")

    modules = element_motion.modules(declarations=[first, second])

    assert modules["pack_one/effects/shared"] == tmp_path / "pack_one/rendering/elements/effects/shared/motion.ts"
    assert modules["pack_two/effects/shared"] == tmp_path / "pack_two/rendering/elements/effects/shared/motion.ts"
    assert "shared" not in modules

    element = SimpleNamespace(
        type="shared",
        clip={"elementRef": {"id": "shared", "kind": "effect", "packId": "pack_two"}},
        audio=False,
    )
    maths = element_motion.ElementMaths([element], declarations=[first, second])
    assert maths.wants(element)
    assert maths._module_key(element) == "pack_two/effects/shared"


def test_undeclared_legacy_motion_is_not_discovered(tmp_path):
    legacy = tmp_path / "legacy" / "elements" / "effects" / "orphan"
    legacy.mkdir(parents=True)
    (legacy / "element.yaml").write_text("id: orphan\nkind: effect\n", encoding="utf-8")
    (legacy / "motion.ts").write_text("export function motionAt() { return {}; }\n", encoding="utf-8")

    assert element_motion.modules(tmp_path) == {}


def test_malformed_declarations_fail_closed_without_displacing_valid_one(tmp_path):
    valid = _declaration(tmp_path, "pack_one", "valid")
    malformed = dict(valid)
    malformed["manifest_path"] = str(tmp_path / "outside" / "element.yaml")

    modules = element_motion.modules(declarations=[malformed, valid])

    assert modules["pack_one/effects/valid"].name == "motion.ts"
    assert list(modules) == ["pack_one/effects/valid", "valid"]


def test_discovery_change_does_not_drop_clip_local_transform_adapter():
    source = element_motion.SCRIPT.read_text(encoding="utf-8")
    assert "clipFrame / fps" in source
    assert "authored canvas pixels" in source


def test_external_only_registry_motion_discovery_and_mirror_fallback(tmp_path, monkeypatch):
    pack_root = tmp_path / "external" / "motion_fixture"
    pack_root.mkdir(parents=True)
    pack_id = _installed_motion_pack(pack_root)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(pack_root.parent))

    # The selected project checkout has no pack directory. The registry's
    # selected external root still owns both installed motion declarations.
    checkout_packs = tmp_path / "checkout" / "astrid" / "packs"
    assert not checkout_packs.exists()
    registry = load_default_registry(project_root=tmp_path / "checkout")
    elements = registry.list(pack_id=pack_id)
    assert {element.id for element in elements} == {
        "installed-animation", "installed-transition"
    }

    modules = element_motion.modules(checkout_packs, elements=elements)
    assert set(modules) == {
        "motion_fixture/animations/installed-animation",
        "motion_fixture/transitions/installed-transition",
        "installed-animation",
        "installed-transition",
    }

    element = SimpleNamespace(
        type="installed-animation",
        clip={"elementRef": {
            "packId": pack_id, "kind": "animations", "id": "installed-animation"
        }},
        audio=False,
        key="fixture@0",
        start=0,
        end=1,
        params={},
    )
    maths = element_motion.ElementMaths([element], types=modules)
    assert maths.wants(element)

    def unavailable(*_args, **_kwargs):
        raise element_motion.Unavailable("fixture runtime unavailable")

    monkeypatch.setattr(element_motion, "evaluate", unavailable)
    assert maths.at(element, 0, 30) is None
    assert maths.note == "fixture runtime unavailable"
