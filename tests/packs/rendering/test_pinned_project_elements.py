"""Pinned source survives admission, registry dispatch, bundling and staging."""
from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.element import catalog, project_packs
from astrid.core.element.registry import ElementRegistry
from astrid.core.rendering import _effect_registry as generator
from astrid.core.rendering.project_elements import (
    CARRIER,
    PACK_ROOTS_ENV,
    REGISTRY_ROOT_ENV,
    selections,
    stage_project_elements,
)
from astrid.core.rendering.storage import used_effect_asset_sizes
from astrid.packs.rendering.actions.render import managed_timeline as managed
from astrid.packs.rendering.rendering.renderers.remotion import run
from astrid.sdk.invocation import (
    _admitted_timeline_element_resources,
    _prepare_managed_render_inputs,
)
from tests.core import test_project_packs
from tests.packs.rendering.test_managed_timeline_render import _Runtime

configured_project_pack = test_project_packs.configured
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def admitted(configured_project_pack):
    runtime = _Runtime()
    runtime.documents = configured_project_pack
    project = runtime.project["id"]
    source, element, manifest = test_project_packs.seed(project)
    definition = yaml.safe_load((element / "element.yaml").read_text())
    definition["assets"] = {"badge": "badge.txt"}
    (element / "element.yaml").write_text(yaml.safe_dump(definition))
    manifest["rendering"]["effects/reveal"]["resources"].append({
        "kind": "asset", "path": "rendering/elements/effects/reveal/badge.txt",
    })
    (source / "pack.yaml").write_text(yaml.safe_dump(manifest))
    refs = []
    for version in ("first", "second"):
        (element / "component.tsx").write_text(
            f"export default function Reveal(){{return '{version}';}}\n"
            f"Reveal.prepareSceneCapability=()=>'{version}';\n")
        (element / "badge.txt").write_text(version)
        record = project_packs.admit_project_pack("realm", project)["elements"][0]
        refs.append({key: record["descriptor"][key] for key in ("packId", "kind", "id", "revision")})
    reads = {"documents": [], "objects": []}

    def verified_document(project, pin):
        reads["documents"].append(copy.deepcopy(pin))
        row = copy.deepcopy(configured_project_pack.rows[project, pin["document_id"]])
        return {"pin": pin, "document": {**row, "project_id": project}}

    def verified_object(project, pin, object_id):
        reads["objects"].append(object_id)
        row = configured_project_pack.rows[project, pin["document_id"]]
        descriptor = next(a["snapshot_object"] for a in row["content"]["admissions"]
                          if a["snapshot_object"]["object_id"] == object_id)
        return {**descriptor, "bytes_base64": base64.b64encode(
            configured_project_pack.media.objects[project, object_id]).decode("ascii")}

    runtime.get_project_verified_document = verified_document
    runtime.get_project_verified_document_object = verified_object
    runtime.documents._client = runtime
    runtime.timeline["config"] = {
        "tracks": [{"id": "v1", "kind": "visual", "label": "V1"}],
        "clips": [{"id": f"clip-{i}", "track": "v1", "clipType": "effect-layer",
                   "at": i, "hold": 1, "elementRef": ref}
                  for i, ref in enumerate([*refs, refs[0]])],
    }
    return runtime, refs, reads


def _snapshot(runtime):
    return managed.resolve_managed_render_snapshot(project_ref="demo", timeline_ref="main", client=runtime)


def _patch_colliding_installed_owner(monkeypatch, ref, source_kind="source", *, exact=False):
    """Model an installed/local owner with a selected current catalog revision."""
    from astrid.core.pack import discovery

    monkeypatch.setattr(
        discovery,
        "discover_pack_metadata",
        lambda **_: (SimpleNamespace(id=ref["packId"], source_kind=source_kind),),
    )
    current_revision = ref["revision"] if exact else "sha256:" + "0" * 64
    monkeypatch.setattr(
        catalog,
        "list_element_descriptors",
        lambda *args, **kwargs: (
            {**ref, "revision": current_revision},
            {**ref, "id": "current-only", "revision": current_revision},
        ),
    )


@pytest.mark.parametrize("source_kind", ["source", "local"])
def test_colliding_installed_owner_uses_exact_retained_snapshot(
    admitted, tmp_path, monkeypatch, source_kind,
):
    runtime, refs, reads = admitted
    ref = refs[0]
    runtime.timeline["config"]["clips"] = runtime.timeline["config"]["clips"][:1]
    with monkeypatch.context() as patch:
        _patch_colliding_installed_owner(patch, ref, source_kind)
        snapshot = _snapshot(runtime)
    assert selections(snapshot.config)[0]["reference"] == ref
    assert len(reads["documents"]) == 1
    assert len(reads["objects"]) == 1
    registry, _ = stage_project_elements(
        json.loads(json.dumps(snapshot.config)), tmp_path / "selected", registry=ElementRegistry()
    )
    element = registry.get("effects", ref["id"], pack_id=ref["packId"], revision=ref["revision"])
    assert "first" in element.component.read_text()


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
@pytest.mark.parametrize("source_kind", ["source", "local"])
def test_colliding_installed_owner_snapshot_failure_is_fail_closed(
    admitted, monkeypatch, failure, source_kind,
):
    runtime, refs, _ = admitted
    ref = refs[0]
    runtime.timeline["config"]["clips"] = runtime.timeline["config"]["clips"][:1]
    with monkeypatch.context() as patch:
        _patch_colliding_installed_owner(patch, ref, source_kind)
        if failure == "missing":
            runtime.documents.rows.clear()
        else:
            original = runtime.get_project_verified_document_object

            def corrupt_snapshot(*args):
                snapshot = original(*args)
                return {**snapshot, "bytes_base64": base64.b64encode(b"corrupt").decode("ascii")}

            runtime.get_project_verified_document_object = corrupt_snapshot
        with pytest.raises(ValueError, match="snapshot (missing|digest mismatch)") as error:
            _snapshot(runtime)
    assert ref["revision"] in str(error.value)
    assert "$.clips[0]" in str(error.value)


@pytest.mark.parametrize("source_kind", ["source", "local"])
def test_exact_catalog_identity_collision_missing_admission_is_fail_closed(
    admitted, monkeypatch, source_kind,
):
    runtime, refs, reads = admitted
    ref = refs[0]
    runtime.timeline["config"]["clips"] = runtime.timeline["config"]["clips"][:1]
    row = next(iter(runtime.documents.rows.values()))
    row["content"]["admissions"] = [
        admission for admission in row["content"]["admissions"]
        if admission["revision"] != ref["revision"]
    ]
    with monkeypatch.context() as patch:
        _patch_colliding_installed_owner(patch, ref, source_kind, exact=True)
        with pytest.raises(ValueError, match="snapshot missing") as error:
            _snapshot(runtime)
    assert ref["revision"] in str(error.value)
    assert "$.clips[0]" in str(error.value)
    assert len(reads["documents"]) == 1
    assert reads["objects"] == []


@pytest.mark.parametrize("source_kind", ["source", "local"])
def test_exact_catalog_identity_collision_owner_document_fails_closed(
    admitted, monkeypatch, source_kind,
):
    runtime, refs, reads = admitted
    ref = refs[0]
    runtime.timeline["config"]["clips"] = runtime.timeline["config"]["clips"][:1]
    row = next(iter(runtime.documents.rows.values()))
    original = runtime.get_project_verified_document_object

    def corrupt_snapshot(*args):
        snapshot = original(*args)
        return {**snapshot, "bytes_base64": base64.b64encode(b"corrupt").decode()}

    runtime.get_project_verified_document_object = corrupt_snapshot
    with monkeypatch.context() as patch:
        _patch_colliding_installed_owner(patch, ref, source_kind, exact=True)
        with pytest.raises(ValueError, match="snapshot (missing|digest mismatch)") as error:
            _snapshot(runtime)
    assert ref["revision"] in str(error.value)
    assert "$.clips[0]" in str(error.value)
    assert len(reads["documents"]) == 1
    assert reads["objects"] == [row["content"]["admissions"][0]["snapshot_object"]["object_id"]]


def test_two_revisions_keep_source_dispatch_aliases_and_assets(admitted, tmp_path):
    runtime, refs, reads = admitted
    snapshot = _snapshot(runtime)
    managed.validate_managed_render_snapshot(snapshot)
    assert len(reads["documents"]) == 1
    assert len(reads["objects"]) == 2
    # Serialization is the real parent/child handoff; execution uses no reader.
    config = json.loads(json.dumps(snapshot.config))
    registry, roots = stage_project_elements(config, tmp_path / "selected", registry=ElementRegistry())
    assert len(roots) == 2
    for ref, version in zip(refs, ("first", "second")):
        element = registry.get("effects", ref["id"], pack_id=ref["packId"], revision=ref["revision"])
        assert version in element.component.read_text()
        assert (element.root / "badge.txt").read_text() == version
        assert ref["revision"].removeprefix("sha256:") in element.metadata["render_key"]
    generated = generator.generate_element_registry("effects", registry=registry)
    for ref in refs:
        assert f"{ref['packId']}@{ref['revision']}" in generated
    generated_path = tmp_path / "registries/src/effects.generated.ts"
    generated_path.parent.mkdir(parents=True)
    generated_path.write_text(generated)
    refs_path = tmp_path / "refs.json"
    refs_path.write_text(json.dumps(refs))
    result = subprocess.run(
        [os.environ.get("ASTRID_NODE_EXECUTABLE", "node"),
         str(Path(__file__).with_name("pinned_project_elements.mjs")),
         str(ROOT / "remotion/webpack-alias.mjs"), str(generated_path), str(refs_path)],
        env={**os.environ, PACK_ROOTS_ENV: json.dumps(roots),
             REGISTRY_ROOT_ENV: str(generated_path.parent.parent)},
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == ["first", "second"]
    summary = run._stage_effect_assets_for_timeline(
        config, project_dir=tmp_path / "host", theme_path=None, render_hash="render",
        registry=registry,
    )
    assert len(summary["effects"]) == 2
    paths = [clip["params"]["__astridAssets"]["badge"] for clip in config["clips"]]
    assert paths[0] != paths[1] and paths[0] == paths[2]
    assert [(tmp_path / "host/public" / path).read_text() for path in paths] == ["first", "second", "first"]
    assert len(used_effect_asset_sizes(config, element_registry=registry)) == 2
    assert len(reads["objects"]) == 2
    with pytest.raises(KeyError, match="revision"):
        registry.get("effects", "reveal", pack_id=refs[0]["packId"], revision="sha256:" + "f" * 64)


@pytest.mark.parametrize("kind,slot", [("animation", "entrance"), ("transition", "transition")])
def test_non_effect_pins_use_revision_slots_and_assets(configured_project_pack, tmp_path, kind, slot):
    from astrid.core.rendering.project_elements import resolve_project_elements

    runtime = _Runtime()
    runtime.documents = configured_project_pack
    source, element, manifest = test_project_packs.seed(runtime.project["id"])
    target = source / "rendering/elements" / (kind + "s") / "reveal"
    target.parent.mkdir()
    element.rename(target)
    definition = yaml.safe_load((target / "element.yaml").read_text())
    definition.update(kind=kind, assets={"badge": "badge.txt"})
    (target / "element.yaml").write_text(yaml.safe_dump(definition))
    declaration = manifest["rendering"].pop("effects/reveal")
    declaration["path"] = declaration["path"].replace("/effects/", f"/{kind}s/")
    declaration["resources"][0]["path"] = declaration["resources"][0]["path"].replace("/effects/", f"/{kind}s/")
    declaration["resources"].append({"kind": "asset", "path": f"rendering/elements/{kind}s/reveal/badge.txt"})
    manifest["rendering"][kind + "s/reveal"] = declaration
    (source / "pack.yaml").write_text(yaml.safe_dump(manifest))
    refs = []
    for version in ("first", "second"):
        (target / "component.tsx").write_text(f"export default function Reveal(){{return '{version}';}}\n")
        (target / "badge.txt").write_text(version)
        record = project_packs.admit_project_pack("realm", runtime.project["id"])["elements"][0]
        refs.append({key: record["descriptor"][key] for key in ("packId", "id", "kind", "revision")})
    row = copy.deepcopy(next(iter(configured_project_pack.rows.values())))
    row["project_id"] = runtime.project["id"]
    pin = {"document_id": row["document_id"], "version": row["version"],
           "content_digest": project_packs._json_digest(row["content"])}
    runtime.get_project_verified_document = lambda *_: {"document": row, "pin": pin}
    runtime.get_project_verified_document_object = lambda project, _pin, object_id: {
        **next(a["snapshot_object"] for a in row["content"]["admissions"] if a["snapshot_object"]["object_id"] == object_id),
        "bytes_base64": base64.b64encode(configured_project_pack.media.objects[project, object_id]).decode(),
    }
    config = resolve_project_elements({"clips": [
        {"id": str(i), "clipType": "media", "elementRef": ref,
         slot: {"id": "reveal", "params": {"authored": True}}} for i, ref in enumerate(refs)
    ]}, project=runtime.project["id"], client=runtime)
    registry, _ = stage_project_elements(config, tmp_path / "selected", registry=ElementRegistry())
    generated = generator.generate_element_registry(kind + "s", registry=registry)
    summary = run._stage_effect_assets_for_timeline(
        config, project_dir=tmp_path / "host", theme_path=None, render_hash="render", registry=registry,
    )
    for clip, selected in zip(config["clips"], selections(config)):
        key = selected["reference"]["revision"].removeprefix("sha256:")
        assert key in clip[slot]["id"] and key in generated
        assert clip[slot]["params"]["authored"] is True
        assert "badge" in clip[slot]["params"]["__astridAssets"]
    assert len(summary[kind + "s"]) == 2
    assert config["clips"][0][slot]["id"] != config["clips"][1][slot]["id"]

@pytest.mark.parametrize("failure", ["document", "admission", "object", "missing-descriptor", "corrupt", "digest"])
def test_missing_or_corrupt_snapshot_never_uses_current_source(admitted, failure):
    runtime, refs, _ = admitted
    row = next(iter(runtime.documents.rows.values()))
    if failure == "document":
        runtime.documents.rows.clear()
    elif failure == "admission":
        row["content"]["admissions"] = [a for a in row["content"]["admissions"] if a["revision"] != refs[0]["revision"]]
    elif failure == "missing-descriptor":
        row["content"]["admissions"][0].pop("snapshot_object")
    else:
        original = runtime.get_project_verified_document_object

        def broken(*args):
            if failure == "object":
                raise FileNotFoundError("retained bytes missing")
            value = original(*args)
            if failure == "corrupt":
                value["bytes_base64"] = base64.b64encode(b"corrupt").decode()
            else:
                value["digest"] = "sha256:" + "f" * 64
            return value

        runtime.get_project_verified_document_object = broken
    with pytest.raises(ValueError, match="snapshot (missing|digest mismatch)") as error:
        _snapshot(runtime)
    assert refs[0]["revision"] in str(error.value)
    assert "$.clips[0]" in str(error.value)


def test_old_pin_ignores_new_catalog_and_materializes_frozen_inputs(admitted, tmp_path, monkeypatch):
    runtime, refs, _ = admitted
    runtime.timeline["config"]["clips"] = runtime.timeline["config"]["clips"][:1]
    original = catalog.list_element_descriptors
    monkeypatch.setattr(catalog, "list_element_descriptors", lambda *args, **kwargs: (
        *original(*args, **kwargs), {**refs[1], "source": "pack:" + refs[1]["packId"]},
    ))
    snapshot = _snapshot(runtime)
    managed.validate_managed_render_snapshot(snapshot)
    assert selections(snapshot.config)[0]["reference"] == refs[0]
    path, _, _ = managed.materialize_managed_render_snapshot(tmp_path, snapshot)
    assert selections(json.loads(path.read_text()))[0]["reference"] == refs[0]
    assert _admitted_timeline_element_resources(snapshot.config) == {}
    assert sum(used_effect_asset_sizes(snapshot.config).values()) == len("first")
    inputs, _ = _prepare_managed_render_inputs(
        {"timeline_ref": "main"}, project="demo", _client=runtime,
    )
    assert selections(inputs["timeline_snapshot"]["config"])[0]["reference"] == refs[0]


def test_frozen_source_is_rechecked_before_staging(admitted, tmp_path):
    runtime, _, _ = admitted
    config = dict(_snapshot(runtime).config)
    selections(config)[0]["snapshot"]["bytes_base64"] = base64.b64encode(b"changed").decode()
    with pytest.raises(ValueError, match="do not match descriptor"):
        stage_project_elements(config, tmp_path / "selected")
    assert not (tmp_path / "selected").exists()


def test_builtin_pin_does_not_fetch_project_snapshot():
    runtime = _Runtime()
    runtime.documents = SimpleNamespace(
        _identity=lambda: {"realm_id": "realm"},
        show=lambda *_args, **_kwargs: SimpleNamespace(
            ok=False, data=None, error=SimpleNamespace(code="not_found"),
        ),
    )
    descriptor = catalog.list_element_descriptors("effects")[0]
    runtime.timeline["config"] = {
        "tracks": [{"id": "v1", "kind": "visual", "label": "V1"}],
        "clips": [{"id": "builtin", "track": "v1", "at": 0, "hold": 1,
                   "clipType": descriptor["id"], "elementRef": {
                       key: descriptor[key] for key in ("packId", "kind", "id", "revision")}}],
        "app": {CARRIER: [{"untrusted": "authored carrier"}]},
    }
    snapshot = _snapshot(runtime)  # The fake intentionally has no project snapshot ports.
    managed.validate_managed_render_snapshot(snapshot)
    assert selections(snapshot.config) == []
    registry = catalog._registry()
    ref = snapshot.config["clips"][0]["elementRef"]
    assert registry.get("effect", ref["id"], pack_id=ref["packId"], revision=ref["revision"]) == registry.get("effect", ref["id"])


def test_render_uses_private_registries_under_global_lock(admitted, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from astrid.core.rendering.remotion_runtime import RemotionRuntimeTools
    from tests.packs.rendering.test_remotion_locking import _FakeMaterializer

    runtime, _, reads = admitted
    snapshot = _snapshot(runtime)
    timeline_path, assets_path, _ = managed.materialize_managed_render_snapshot(tmp_path, snapshot)
    host = tmp_path / "host"
    (host / "src").mkdir(parents=True)
    original = host / "src/effects.generated.ts"
    original.write_text("settled host registry\n")
    monkeypatch.setattr(run.remotion_lock, "REMOTION_LOCK_PATH", tmp_path / "render.lock")
    monkeypatch.setattr(run, "_validate_project_dir", lambda _: RemotionRuntimeTools(
        Path("/trusted/node"), "v20.19.4", host / "node_modules/@remotion/cli/remotion-cli.js"))
    monkeypatch.setattr(run, "load_default_registry", lambda **_: ElementRegistry())
    monkeypatch.setattr(run, "_available_remotion_port", lambda: 43210)
    monkeypatch.setattr(run, "AssetMaterializer", _FakeMaterializer)
    monkeypatch.setattr(run, "_require_free_space", lambda *_: None)
    monkeypatch.setattr(run, "_resolved_theme_for_render", lambda *_: {"id": "test", "visual": {}})
    monkeypatch.setattr(run, "_canonical_profile", lambda *_: SimpleNamespace(fps_rational=(30, 1)))
    request = run.RenderRequest(
        schema_version=1, timeline_path=str(timeline_path), output_name="video.mp4",
        assets_registry_path=str(assets_path),
        backend_config={run.BACKEND_ID: {"project_dir": str(host)}},
    )
    report = run.support(request, workspace=tmp_path)
    assert report.supported, report.reasons
    observed = {}

    def render(command, **kwargs):
        assert run.remotion_lock.remotion_render_lock_held()
        env = kwargs["env"]
        selected_root = Path(env[REGISTRY_ROOT_ENV])
        assert selected_root.is_relative_to(tmp_path)
        assert (selected_root / "src/effects.generated.ts").is_file()
        assert len(json.loads(env[PACK_ROOTS_ENV])) == 2
        props = json.loads(Path(command[command.index("--props") + 1]).read_text())
        assert CARRIER not in props["timeline"]["app"]
        assert len({clip["params"]["__astridAssets"]["badge"] for clip in props["timeline"]["clips"]}) == 2
        observed["root"] = selected_root
        output = Path(command[command.index("--output") + 1])
        (output / "frame-0.png").write_bytes(b"frame")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(run.subprocess, "run", render)
    with run.remotion_lock.remotion_render_lock():
        details = run._execute_remotion_locked(
            timeline_path, assets_path, tmp_path / "video.mp4",
            provenance_out_path=tmp_path / "video.mp4", project_dir=host,
            composition_id="PinnedTest", theme_path=None, min_free_gb=None,
            frame_numbers=[0], frame_output_dir=tmp_path / "frames",
        )
    assert not observed["root"].exists()
    assert original.read_text() == "settled host registry\n"
    assert not Path(details.stage_summary["root"]).exists()
    assert len(reads["objects"]) == 2
    corrupted = json.loads(timeline_path.read_text())
    selections(corrupted)[0]["snapshot"]["bytes_base64"] = base64.b64encode(b"changed").decode()
    timeline_path.write_text(json.dumps(corrupted))
    report = run.support(request, workspace=tmp_path)
    assert not report.supported
    assert any("snapshot" in reason and "do not match descriptor" in reason for reason in report.reasons)
    corrupted["app"].pop(CARRIER)
    timeline_path.write_text(json.dumps(corrupted))

    def refuse_catalog_fallback(**kwargs):
        raise AssertionError("missing snapshot must not fall back to the current registry")

    monkeypatch.setattr(run, "load_default_registry", refuse_catalog_fallback)
    report = run.support(request, workspace=tmp_path)
    assert not report.supported
    assert any("snapshot missing" in reason for reason in report.reasons)
