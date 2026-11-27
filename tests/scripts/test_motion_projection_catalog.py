"""RN-5b-min catalog replacement preserves all non-motion descriptor bytes."""

import json

import pytest

from scripts.project_astrid_motion_catalog import replace_motion_rows


def test_replaces_only_motion_rows_and_retains_other_bytes():
    other = '{"packId":"rendering", "id":"fade", "kind":"transition", "duration":1.0}'
    old = '{"packId":"astrid_motion", "id":"am-type", "kind":"effect", "revision":"old"}'
    source = "prefix\nObject.freeze(\n[\n  " + other + ",\n  " + old + "\n]\n);\nsuffix\n"
    replacement = {"packId": "astrid_motion", "id": "am-type", "kind": "effect", "revision": "new"}
    result = replace_motion_rows(source, [replacement])
    assert other in result
    assert old not in result
    assert result.startswith("prefix\nObject.freeze(\n")
    assert result.endswith("\n);\nsuffix\n")
    rows = json.loads(result.split("Object.freeze(\n")[1].split("\n);\n")[0])
    assert rows[1] == replacement
    assert replace_motion_rows(result, [replacement]) == result


def test_rejects_missing_motion_rows():
    source = 'Object.freeze(\n[{"packId":"astrid_motion","kind":"effect","id":"old"}]\n);\n'
    with pytest.raises(ValueError, match="Unexpected"):
        replace_motion_rows(source, [{"packId": "astrid_motion", "kind": "effect", "id": "new"}])


def test_direct_catalog_uses_product_descriptors_and_preserves_other_rows(tmp_path, monkeypatch):
    import shutil
    from astrid.core.element.catalog import _element_descriptor
    from astrid.core.element.registry import _load_pack_elements_with_diagnostics
    from astrid.core.pack import ELEMENT_KIND_REGISTRY, load_pack_manifest
    from scripts import project_astrid_motion as projection
    from scripts import project_astrid_motion_catalog as catalog

    root = tmp_path / "astrid_motion"
    shutil.copytree(projection.ROOT / "tests/fixtures/packs/astrid_motion", root)
    digest = projection.pack_digest(root)
    # Independent fixture identity; production K/T/D are never supplied here.
    record = {
        "schema": "astrid-motion-release-source/v1", "pack_id": "astrid_motion",
        "repo": "https://github.com/banodoco/poms_packs", "upstream_commit": "c" * 40,
        "upstream_subpath": "astrid_motion", "upstream_tree": "d" * 40,
        "digest_rule": "pack-tree-v1", "upstream_digest": digest, "tree_digest": digest,
        "file_count": len([p for p in root.rglob("*") if p.is_file()]), "transform": "identity",
    }
    resource = tmp_path / "astrid_motion_release_source.json"
    resource.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(projection, "RELEASE_SOURCE_PATH", resource)
    rows = catalog.motion_rows(root, profile="direct-source")
    pack = load_pack_manifest(root / "pack.yaml", expected_pack_id="astrid_motion")
    elements, skipped = _load_pack_elements_with_diagnostics((pack,), element_kind_registry=ELEMENT_KIND_REGISTRY)
    assert not skipped
    assert len(rows) == len(elements) == 20
    expected = []
    for element in elements:
        item = _element_descriptor(element)
        item["componentPath"] = "packs/astrid_motion/" + element.component.relative_to(root).as_posix()
        expected.append(item)
    assert rows == expected
    assert all(row["revision"] for row in rows)
    other = '{"packId":"rendering", "id":"fade", "kind":"transition", "duration":1.0}'
    original = "prefix\nObject.freeze(\n[" + other + "," + ",".join(json.dumps({
        "packId": "astrid_motion", "kind": r["kind"], "id": r["id"], "revision": "old",
    }) for r in rows) + "]\n);\nsuffix\n"
    updated = catalog.replace_motion_rows(original, rows)
    assert other in updated
    assert updated.startswith("prefix\nObject.freeze(\n") and updated.endswith("\n);\nsuffix\n")
    assert catalog.replace_motion_rows(updated, rows) == updated
    assert projection.pack_digest(root) == digest
    with pytest.raises(ValueError, match="digest mismatch"):
        catalog.motion_rows(root)  # Explicit direct profile is mandatory.
    component = root / "rendering/elements/effects/am-type/component.tsx"
    component.write_bytes(component.read_bytes() + b"\n// changed\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        catalog.motion_rows(root, profile="direct-source")
