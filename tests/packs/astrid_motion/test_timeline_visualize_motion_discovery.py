"""Cheap, render-free proof for admitted V3 motion-resource discovery."""

from __future__ import annotations

from types import SimpleNamespace

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
