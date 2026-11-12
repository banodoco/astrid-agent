"""Element params are checked against the pack that declares the element, wherever it is installed.

With the declaring pack present, unknown keys and invalid values are refused (R10). With no resolvable
schema, only PARAMETER WRITES are refused, each cause with its own code; reading, inspecting, unchanged
params and schema-independent edits keep working, and the clip's opaque params are kept byte for byte."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.element import registry as element_registry
from astrid.core.element.registry import clear_default_registry_cache
from astrid.sdk import timeline_address
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_address import (
    ElementManifestInvalid,
    ElementPackNotInstalled,
    ElementRegistryUnavailable,
    describe_target,
    element_schema,
    resolve,
)
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import SheetError, apply_sheet, render_sheet
from tests.sdk.test_timeline_sync import connected as connected

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"
ELEMENT = "zz-installed-probe"
PACK = "zz_probe_pack"
INSTALL = "astrid packs add https://github.com/banodoco/poms_packs --pack-id astrid_motion --subpath astrid_motion"


@pytest.fixture(autouse=True)
def _isolate_schema_caches(monkeypatch):
    monkeypatch.setattr(timeline_address, "_SCHEMAS", {})
    clear_default_registry_cache()
    yield
    clear_default_registry_cache()


def _probe_pack(collection: Path, *, element_manifest: str | None = None) -> None:
    prefix = f"rendering/elements/effects/{ELEMENT}"
    element = collection / PACK / prefix
    element.mkdir(parents=True)
    (element / "component.tsx").write_text("export default function Probe() { return null; }\n")
    (element / "element.yaml").write_text(element_manifest if element_manifest is not None else json.dumps({
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


def _uses(monkeypatch, root: Path | None) -> None:
    if root is None:
        monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    else:
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(root))
    monkeypatch.setattr(timeline_address, "_SCHEMAS", {})
    clear_default_registry_cache()


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


def _tiny(element: str | None = None) -> Checkout:
    bundle = copy.deepcopy(json.loads(TINY.read_text(encoding="utf-8")))
    if element is not None:
        rocket = _find_clip(bundle, "a-rocket")
        rocket["clipType"], rocket["params"] = element, {"size": 2}
    tl = Checkout(bundle)
    tl.resolve()
    return tl


def _am_params(tl: Checkout) -> dict[str, str]:
    return {c.id: json.dumps(c.data.get("params"), sort_keys=True) for c in tl.clips() if c.element.startswith("am-")}


# --------------------------------------------------------------- pack present: strict, as at R10

def test_an_installed_pack_declares_the_schema_and_every_check_uses_it(tmp_path, monkeypatch):
    _probe_pack(tmp_path / "installed")
    _uses(monkeypatch, tmp_path / "installed")
    assert sorted(element_schema(ELEMENT)["properties"]) == ["mode", "size"]
    clip = _tiny(ELEMENT).clip("c1.rocket")
    clip.set(size=3)
    assert clip.params["size"] == 3
    with pytest.raises(TimelineEditError, match=r"c1\.rocket\.size: 9 is above its maximum 8"):
        clip.set(size=9)
    with pytest.raises(TimelineEditError, match=r'c1\.rocket\.size: "big" is not an integer'):
        clip.set(size="big")
    with pytest.raises(TimelineEditError, match="has no param zzbogus"):
        clip.set(zzbogus=1)
    assert clip.params == {"size": 3}


def test_a_cached_schema_stays_on_its_provider_until_its_identity_changes(tmp_path, monkeypatch):
    _probe_pack(tmp_path / "installed")
    _uses(monkeypatch, tmp_path / "installed")
    first = element_schema(ELEMENT)
    assert element_schema(ELEMENT) is first


def test_removing_a_cached_provider_refuses_parameter_writes_after_registry_clear(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    pack = installed / PACK
    _probe_pack(installed)
    _uses(monkeypatch, installed)
    tl = _tiny(ELEMENT)
    assert element_schema(ELEMENT)["properties"]["size"]["maximum"] == 8

    import shutil

    shutil.rmtree(pack)
    clear_default_registry_cache()
    with pytest.raises(ElementPackNotInstalled) as caught:
        tl.clip("c1.rocket").set(size=3)
    assert caught.value.code == "element_pack_not_installed"


def test_replacing_a_cached_provider_with_a_malformed_manifest_refuses_with_its_code(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    pack = installed / PACK
    _probe_pack(installed)
    _uses(monkeypatch, installed)
    tl = _tiny(ELEMENT)
    assert element_schema(ELEMENT)["properties"]["size"]["maximum"] == 8
    assert timeline_address._SCHEMA_BINDINGS[ELEMENT][0] == PACK

    (pack / "pack.yaml").write_text('{"schema_version": 3, "id": "broken"')
    with pytest.raises(ElementManifestInvalid) as caught:
        tl.clip("c1.rocket").set(size=3)
    assert caught.value.code == "element_manifest_invalid"


# --------------------------------------------------------------- pack absent

def test_a_moved_element_names_its_pack_and_install_command_and_writes_nothing(monkeypatch):
    _uses(monkeypatch, None)
    tl = _tiny()
    before = _am_params(tl)
    clip = tl.clip("c1.rocket")
    for edit in ({"x": 120}, {"x": "not-a-number"}, {"zzbogus": 2}, {"scale": None}):
        with pytest.raises(ElementPackNotInstalled) as caught:
            clip.set(**edit)
        assert caught.value.code == "element_pack_not_installed"
        assert caught.value.cause["details"] == {"element": "am-sprite", "pack_id": "astrid_motion",
                                                 "install": INSTALL}
        assert f"`{INSTALL}`" in str(caught.value)
        assert "Moved to poms_packs" in str(caught.value)
    with pytest.raises(ElementPackNotInstalled):
        clip.set_param("x", 5)
    assert _am_params(tl) == before


def test_without_the_pack_a_timeline_still_shows_inspects_and_round_trips_unchanged(monkeypatch):
    _uses(monkeypatch, None)
    tl = _tiny()
    before = json.dumps(tl.document(), sort_keys=True)
    sheet = render_sheet(tl)
    assert "rocket" in sheet and "snap-plate" in sheet
    shown = describe_target(tl, resolve(tl, "c1.rocket"), full=True)
    assert "[element_pack_not_installed]" in shown and INSTALL in shown
    assert "x" in shown  # the stored params are still listed
    describe_target(tl, resolve(tl, "c1.rocket.x"))
    clip = tl.clip("c1.rocket")
    clip.set(x=clip.get("x"))
    clip.set_param("x", clip.get("x"))
    apply_sheet(tl, sheet)  # every row as shown: nothing to write, nothing refused
    assert json.dumps(tl.document(), sort_keys=True) == before


def test_without_the_pack_schema_independent_edits_work_and_keep_the_params_exactly(monkeypatch):
    _uses(monkeypatch, None)
    tl = _tiny()
    before = _am_params(tl)
    tl.clip("c1.rocket").set(opacity=0.5)  # a clip-level look field, not an element param
    assert tl.clip("c1.rocket").data.get("opacity") == 0.5 or "opacity" in json.dumps(tl.clip("c1.rocket").data)
    tl.clip("c1.rocket").hold_for(2.0)
    assert _am_params(tl) == before
    edited = render_sheet(tl).replace("x=60", "x=66", 1)
    with pytest.raises(SheetError, match=r"\[element_pack_not_installed\] am-sprite"):
        apply_sheet(tl, edited)
    assert _am_params(tl) == before


@pytest.mark.parametrize("via", ["cli", "python", "sheet"])
@pytest.mark.parametrize("assignment", ["x=120", "x='not-a-number'", "zzbogus=2"])
def test_absent_pack_refuses_each_frontend_with_the_dependency_diagnostic(monkeypatch, via, assignment):
    from tests.editing_conformance.runner import parse_scenario, run_case

    _uses(monkeypatch, None)
    key, value = assignment.split("=", 1)
    scenario = parse_scenario({
        "id": "look.set.absent-pack", "source": "new", "fixture": "tiny",
        "intent": "Refuse a parameter write until its declaring pack is installed.", "expect": "refused",
        "cli": f"edit --clip c1.rocket --set {assignment}",
        "python": f'tl.clip("c1.rocket").set({key}={value})',
        "sheet": {"cuts": ["c1"], "replace": [["x=60", f"x=60 {assignment}" if key != "x" else assignment]]},
    })
    result = run_case(scenario, via)
    diagnostic = result.stderr + (result.error or "")
    assert "element_pack_not_installed" in diagnostic
    assert f"install with `{INSTALL}`" in diagnostic
    assert result.base == result.after
    assert result.new_steps == 0


def test_opaque_data_keeps_its_exact_json_bytes_without_the_pack(monkeypatch):
    _uses(monkeypatch, None)
    tl = _tiny()
    clip = tl.clip("c1.rocket")
    clip.params["future"] = {"z": [None, True, 1.0, -0.0, {"unicode": "雪"}], "a": "opaque"}
    encode = lambda: json.dumps(clip.data, ensure_ascii=False, separators=(",", ":")).encode()
    before = encode()
    describe_target(tl, resolve(tl, "c1.rocket"), full=True)
    apply_sheet(tl, render_sheet(tl))
    assert encode() == before
    params_before = json.dumps(clip.params, ensure_ascii=False).encode()
    clip.hold_for(2.0)
    assert json.dumps(clip.params, ensure_ascii=False).encode() == params_before


def test_a_sheet_boolean_is_not_an_unchanged_opaque_number(monkeypatch):
    _uses(monkeypatch, None)
    tl = _tiny()
    clip = tl.clip("c1.rocket")
    clip.params["future"] = 1
    sheet = render_sheet(tl).replace("future=1", "future=true", 1)
    assert "future=true" in sheet
    with pytest.raises(SheetError, match="element_pack_not_installed"):
        apply_sheet(tl, sheet)
    assert type(clip.params["future"]) is int


def test_resolving_timing_keeps_unresolved_params_and_their_formulas_opaque(monkeypatch):
    from astrid.sdk import timeline_intent as intent

    _uses(monkeypatch, None)
    tl = _tiny()
    clip = tl.clip("c1.rocket")
    clip.params["future"] = 123.0
    intent.set_formula(clip.data, "params.future", {"moment": "c1 +1s", "as": "clip_frame",
                                                  "opaque": [None, False, 1.0]})
    before = json.dumps((clip.params, intent.formulas(clip.data))).encode()
    tl.resolve()
    clip.hold_for(2.0)
    tl.resolve()
    apply_sheet(tl, render_sheet(tl))
    assert json.dumps((clip.params, intent.formulas(clip.data))).encode() == before


def test_baseline_exemption_preserves_opaque_params_but_refuses_changes_and_removals(monkeypatch):
    from astrid.sdk.timeline.validation import FinalCandidateError, _validate_params

    _uses(monkeypatch, None)
    baseline = {"clipType": "am-sprite", "params": {"opaque": [None, {"v": "unknown"}]}}
    unchanged = copy.deepcopy(baseline)
    _validate_params(unchanged, path="clip", baseline=baseline, schema_inputs=(), realm_id=None, project="test")
    assert unchanged == baseline
    _validate_params({"clipType": "am-sprite"}, path="clip", baseline=None,
                     schema_inputs=(), realm_id=None, project="test")
    for params in ({}, {"opaque": "changed"}):
        with pytest.raises(FinalCandidateError, match="element_pack_not_installed"):
            _validate_params({**baseline, "params": params}, path="clip", baseline=baseline,
                             schema_inputs=(), realm_id=None, project="test")


def test_native_equal_parameter_write_keeps_opaque_bytes_and_the_write_guard(monkeypatch):
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.commands import execute
    from tests.sdk.test_timeline_sync import initial_bundle

    _uses(monkeypatch, None)
    original = initial_bundle()
    clip = State(original).entity("parent/clip:a")
    clip["clipType"] = "am-sprite"
    clip["params"] = {"x": 1, "opaque": [None, True, 1.0, -0.0]}
    before = json.dumps(original).encode()
    call = {"schema_version": 1, "op": "clip.set-params", "version": "1",
            "select": "parent/clip:a", "args": {"values": {"x": 1.0}, "clear": []}}
    result = execute(original, [call])
    assert json.dumps(result.bundle).encode() == before
    assert result.records[0]["writes"]  # an explicit no-op write still guards against an intervening write
    with pytest.raises(ElementPackNotInstalled):
        execute(original, [{**call, "args": {"values": {"x": 2}, "clear": []}}])


def test_attached_unchanged_parameters_work_without_the_pack(connected, monkeypatch):
    from astrid.sdk.timeline._command_state import State
    from tests.sdk.test_timeline_native_checkout import attached, pending

    _uses(monkeypatch, None)
    row = State(connected[1].bundles["r0"]).entity("parent/clip:a")
    row["clipType"], row["params"] = "am-sprite", {"x": 10, "opaque": [None, 1.0]}
    tl = attached(connected)
    clip = tl.clip("a")
    before = json.dumps(clip.data).encode()
    clip.set(x=60)
    assert json.dumps(clip.data).encode() == before
    assert pending(connected)["pending"][0]["op"] == "clip.set-params"
    with pytest.raises(ElementPackNotInstalled):
        clip.set(x=120)
    assert json.dumps(clip.data).encode() == before


def test_a_present_but_malformed_pack_manifest_has_its_own_code(tmp_path, monkeypatch):
    broken = tmp_path / "installed" / "astrid_motion"
    broken.mkdir(parents=True)
    (broken / "pack.yaml").write_text('{"schema_version": 3, "id": "astrid_motion"')  # truncated JSON
    _uses(monkeypatch, tmp_path / "installed")
    with pytest.raises(ElementManifestInvalid) as caught:
        element_schema("am-sprite")
    assert caught.value.code == "element_manifest_invalid"
    assert caught.value.cause["details"]["pack_id"] == "astrid_motion"
    assert caught.value.cause["details"]["path"] == str(broken / "pack.yaml")
    assert "not installed" not in str(caught.value)


def test_a_malformed_element_manifest_has_its_own_code(tmp_path, monkeypatch):
    _probe_pack(tmp_path / "installed", element_manifest="id: [unterminated\n")
    _uses(monkeypatch, tmp_path / "installed")
    with pytest.raises(ElementManifestInvalid) as caught:
        element_schema(ELEMENT)
    assert caught.value.code == "element_manifest_invalid"
    assert caught.value.cause["details"]["pack_id"] == PACK


def test_a_registry_failure_has_its_own_code(monkeypatch):
    _uses(monkeypatch, None)

    def broken(**_):
        raise RuntimeError("corpus unreadable")

    monkeypatch.setattr(element_registry, "load_default_registry", broken)
    with pytest.raises(ElementRegistryUnavailable) as caught:
        element_schema("am-sprite")
    assert caught.value.code == "element_registry_unavailable"


def test_an_unknown_element_is_not_reported_as_a_moved_pack(monkeypatch):
    _uses(monkeypatch, None)
    with pytest.raises(ElementPackNotInstalled) as caught:
        element_schema(ELEMENT)
    assert caught.value.code == caught.value.cause["code"] == "element_pack_not_installed"
    assert caught.value.cause["details"]["element"] == ELEMENT
    assert "pack_id" not in caught.value.cause["details"]
    clip = _tiny(ELEMENT).clip("c1.rocket")
    for edit in ({"size": 3}, {"size": "not-a-number"}, {"zzbogus": 2}):
        with pytest.raises(ElementPackNotInstalled, match=r"\[element_pack_not_installed\] zz-installed-probe"):
            clip.set(**edit)
    assert clip.params == {"size": 2}
    assert ELEMENT not in timeline_address._SCHEMAS  # a refusal is not cached as a schema


def test_a_media_clip_is_not_an_element_and_keeps_its_fade_params():
    assert element_schema("media") == {}
    assert timeline_address.declared_params("media") == sorted(timeline_address.MEDIA_PARAMS)


def test_text_alias_set_clear_create_and_unchanged_preserve_legacy_data(monkeypatch):
    from astrid.sdk.timeline.validation import FinalCandidateError, validate_final_candidate
    from astrid.sdk.timeline._clip_commands import capture_clip_add
    from astrid.sdk.timeline.commands import execute
    from tests.editing_conformance.test_recipe_m2_clip_add import with_track, literal_row

    _uses(monkeypatch, None)
    assert element_schema("text") == element_schema("text-card")
    base = with_track()
    row = literal_row(params={"text": "Hello world", "opaque": {"future": [None, False, "雪"]}})
    original = execute(base, [capture_clip_add(base, "parent", row)]).bundle
    tl = Checkout(copy.deepcopy(original))
    clip = tl.clip("first-clip")
    before = copy.deepcopy(tl.bundle)
    clip.set(text="Hello world")
    assert tl.bundle == before
    validate_final_candidate(tl.bundle, baseline=before)
    clip.set(content="Caption")
    assert clip.params["content"] == "Caption"
    clip.set(content=None)
    assert "content" not in clip.params
    assert clip.params["opaque"] == row["params"]["opaque"]
    assert clip.element == "text"
    validate_final_candidate(tl.bundle, baseline=before)
    new_row = literal_row(id="new", params={"content": "New"}, at=4, hold=1)
    created = execute(before, [capture_clip_add(before, "parent", new_row)]).bundle
    validate_final_candidate(created, baseline=before)
    assert created["parent"]["clips"][-1]["clipType"] == "text"
    created["parent"]["clips"][-1]["params"]["align"] = "invalid"
    with pytest.raises(FinalCandidateError, match="align"):
        validate_final_candidate(created, baseline=before)
    created["parent"]["clips"][-1]["clipType"] = "zz-unknown-element"
    with pytest.raises(FinalCandidateError, match="element_pack_not_installed"):
        validate_final_candidate(created, baseline=before)
    with pytest.raises(ElementPackNotInstalled):
        element_schema("zz-unknown-element")


def test_default_add_text_uses_bundled_content_without_motion(monkeypatch):
    from tests.packs.timeline.test_parent_clips import media_only

    _uses(monkeypatch, None)
    with pytest.raises(ElementPackNotInstalled):
        element_schema("am-type")
    tl = Checkout(media_only())
    clip = tl.add_text("Caption", at=4, hold=2)
    assert clip.element == "text-card"
    assert clip.params == {"content": "Caption"}
    assert clip.text == "Caption"
    clip.set(content="Edited")
    assert tl.clip("Edited").id == clip.id


def test_moved_element_recovery_command_uses_real_pack_parser(monkeypatch):
    import shlex
    from astrid.core.pack.cli_parser import build_parser

    _uses(monkeypatch, None)
    with pytest.raises(ElementPackNotInstalled) as caught:
        element_schema("am-type")
    command = caught.value.cause["details"]["install"]
    assert f"`{command}`" in str(caught.value)
    words = shlex.split(command)
    assert words[:2] == ["astrid", "packs"]
    args = build_parser().parse_args(words[2:])
    assert args.source == "https://github.com/banodoco/poms_packs"
    assert args.pack_id == "astrid_motion"
    assert args.subpath == "astrid_motion"
    # No revision is pinned: the add command resolves and shows the revision before asking for consent.
    assert args.revision is None
