"""Motion discovery distinguishes the installed package from selected packs."""

from types import SimpleNamespace

import pytest

from astrid.packs.rendering.actions.timeline_visualize.motion import element_motion


@pytest.mark.parametrize("use_builtin_root", [False, True])
def test_package_boundary_preserves_selected_external_roots(tmp_path, monkeypatch, use_builtin_root):
    package_root = tmp_path / "installed" / "astrid"
    bundled = package_root / "packs" / "bundled"
    external = tmp_path / "selected" / "external"
    roots = {"bundled": bundled, "external": external}
    elements = [
        SimpleNamespace(
            metadata={"pack_id": pack_id}, source=f"pack:{pack_id}",
            root=root / "ui" / "elements" / "effects" / "example",
        )
        for pack_id, root in roots.items()
    ]
    registry = SimpleNamespace(list=lambda **kwargs: elements)
    monkeypatch.setattr("astrid.core.element.registry.load_default_registry", lambda **kwargs: registry)
    monkeypatch.setattr(element_motion, "astrid_root", lambda: package_root, raising=False)
    monkeypatch.setattr(element_motion, "_pack_declarations", lambda root, pack_id: [{"pack": pack_id, "root": root}])
    packs_root = package_root / "packs" if use_builtin_root else tmp_path / "project" / "packs"

    declarations = element_motion._default_declarations(packs_root)

    expected = roots if use_builtin_root else {"external": external}
    assert declarations == tuple({"pack": pack_id, "root": root} for pack_id, root in expected.items())
