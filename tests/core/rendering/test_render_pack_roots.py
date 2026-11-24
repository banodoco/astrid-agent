from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.rendering import _effect_registry as generator
from astrid.core.rendering import remotion_runtime


def _plugin(tmp_path: Path, pack: str, element: str, scope: str):
    pack_root = tmp_path / pack
    root = pack_root / "elements" / "effects" / element
    root.mkdir(parents=True)
    (pack_root / "pack.yaml").write_text("id: test\n")
    (root / "component.tsx").write_text("export default 1")
    return generator.PluginRecord(element, "effects", "pack:test", root, {}, {}, "hash", scope)


def test_roots_include_shadowed_effect_owner_and_reject_scope_conflict(tmp_path, monkeypatch):
    winner = _plugin(tmp_path, "first", "title", "pack-first-elements")
    owner = _plugin(tmp_path, "second", "title", "pack-second-elements")
    registry = SimpleNamespace(list=lambda **_: (SimpleNamespace(id=owner.plugin_id, plugin=owner),))
    monkeypatch.setattr(generator, "discover_plugins", lambda kind, **_: {"title": winner} if kind == "effects" else {})
    monkeypatch.setattr(generator, "_component_is_host_importable", lambda _: True)
    monkeypatch.setattr(generator, "_plugin_from_element", lambda element: element.plugin)
    assert generator.render_pack_roots(registry=registry) == [
        {"root": str(tmp_path / "first"), "scope": "pack-first-elements"},
        {"root": str(tmp_path / "second"), "scope": "pack-second-elements"},
    ]
    conflicting = generator.PluginRecord(owner.plugin_id, owner.kind, owner.scope, owner.root,
                                          owner.meta, owner.defaults, owner.fingerprint,
                                          "pack-first-elements")
    registry = SimpleNamespace(list=lambda **_: (SimpleNamespace(id=conflicting.plugin_id, plugin=conflicting),))
    with pytest.raises(ValueError, match="conflicting render import scope"):
        generator.render_pack_roots(registry=registry)


def test_registry_inputs_bind_root_scope_mapping():
    observation = {"module_files": [], "roots": [], "package_roots": [],
                   "optional_files": [], "search_paths": [],
                   "render_pack_roots": [{"root": "/pack", "scope": "first"}]}
    before = remotion_runtime._registry_inputs(observation)
    observation["render_pack_roots"] = [{"root": "/pack", "scope": "second"}]
    assert before != remotion_runtime._registry_inputs(observation)
