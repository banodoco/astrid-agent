"""A clip's element params are checked against the pack that declares the element, wherever that pack is
installed (an element that moved out of this tree resolves from its installed pack). An element no installed
pack declares is refused with a coded error on every write path; it is never edited as schema-less."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.element.registry import clear_default_registry_cache
from astrid.sdk import timeline_address
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_address import ElementSchemaUnavailable, element_schema
from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"
ELEMENT = "zz-installed-probe"
PACK = "zz_probe_pack"


def _install(collection: Path) -> None:
    prefix = f"rendering/elements/effects/{ELEMENT}"
    element = collection / PACK / prefix
    element.mkdir(parents=True)
    (element / "component.tsx").write_text("export default function Probe() { return null; }\n")
    (element / "element.yaml").write_text(json.dumps({
        "id": ELEMENT, "kind": "effect", "runtime": {"adapter": "remotion"},
        "schema": {"type": "object", "additionalProperties": False, "properties": {
            "size": {"type": "integer", "minimum": 1, "maximum": 8},
            "mode": {"type": "string", "enum": ["on", "off"]},
        }},
        "defaults": {"size": 2, "mode": "on"},
    }))
    (collection / PACK / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "pack_api": "2.0", "id": PACK, "name": PACK, "version": "1.0.0",
        "status": "active", "documentation": {"kind": "none", "reason": "Test fixture."},
        "rendering": {f"effects/{ELEMENT}": {
            "type": "element", "path": f"{prefix}/element.yaml",
            "resources": [{"path": f"{prefix}/component.tsx", "kind": "implementation"}],
        }},
    }))


@pytest.fixture(autouse=True)
def fresh_resolution(monkeypatch):
    monkeypatch.setattr(timeline_address, "_SCHEMAS", {})
    clear_default_registry_cache()
    yield
    clear_default_registry_cache()


def _probe_timeline() -> Checkout:
    bundle = copy.deepcopy(json.loads(TINY.read_text(encoding="utf-8")))
    rocket = _find_clip(bundle, "a-rocket")
    rocket["clipType"], rocket["params"] = ELEMENT, {"size": 2}
    tl = Checkout(bundle)
    tl.resolve()
    return tl


def _find_clip(node, clip_id):
    if isinstance(node, dict):
        if node.get("id") == clip_id and "clipType" in node:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = _find_clip(item, clip_id)
            if found is not None:
                return found
    return None


def test_an_installed_pack_declares_the_schema_and_every_check_uses_it(tmp_path, monkeypatch):
    _install(tmp_path / "installed")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(tmp_path / "installed"))
    assert sorted(element_schema(ELEMENT)["properties"]) == ["mode", "size"]
    tl = _probe_timeline()
    clip = tl.clip("c1.rocket")
    clip.set(size=3)
    assert clip.params["size"] == 3
    with pytest.raises(TimelineEditError, match=r"c1\.rocket\.size: 9 is above its maximum 8"):
        clip.set(size=9)
    with pytest.raises(TimelineEditError, match=r'c1\.rocket\.size: "big" is not an integer'):
        clip.set(size="big")
    with pytest.raises(TimelineEditError, match="has no param zzbogus"):
        clip.set(zzbogus=1)
    assert clip.params == {"size": 3}


def test_an_element_no_installed_pack_declares_is_refused_with_a_code_and_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(tmp_path / "empty"))
    with pytest.raises(ElementSchemaUnavailable) as caught:
        element_schema(ELEMENT)
    assert caught.value.code == "element_schema_unavailable"
    assert caught.value.cause["code"] == "element_schema_unavailable"
    assert caught.value.cause["details"]["element"] == ELEMENT
    tl = _probe_timeline()
    clip = tl.clip("c1.rocket")
    for edit in ({"size": 3}, {"size": "not-a-number"}, {"zzbogus": 2}):
        with pytest.raises(ElementSchemaUnavailable, match=r"\[element_schema_unavailable\] zz-installed-probe"):
            clip.set(**edit)
    assert clip.params == {"size": 2}
    assert ELEMENT not in timeline_address._SCHEMAS  # a refusal is not cached as a schema


def test_a_media_clip_is_not_an_element_and_keeps_its_fade_params():
    assert element_schema("media") == {}
    assert timeline_address.declared_params("media") == sorted(timeline_address.MEDIA_PARAMS)
