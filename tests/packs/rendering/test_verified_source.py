"""Runtime-admitted source handoff at the host boundary; no renderer required."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.rendering.contracts import RenderRequest
from astrid.core.rendering.errors import RendererException
from astrid.core.rendering.service import RenderService
from astrid.core.rendering.verified_source import (
    SOURCE_ENV,
    PreparedTimelineInputs,
    VerifiedTimelineSource,
    inherited_source,
)


def admitted(tmp_path, *, capture=False, working_copy=False, media=False, config=None):
    """A Runtime claim double; history/admission itself is not qualified here."""
    config = config or {"tracks": [{"id": "v", "kind": "visual", "label": "Visual"}], "clips": []}
    authority = {"project_id": "project", "timeline_id": "timeline", "parent_revision_id": "old-parent",
                 "parent_content_digest": "sha256:" + "a" * 64,
                 "expansion": {"shots": [{"shot_id": "shot", "revision_id": "shot-1", "content_digest": "sha256:" + "b" * 64}],
                               "children": [{"timeline_id": "internal", "revision_id": "internal-1", "config_hash": "sha256:" + "c" * 64}]}}
    snapshot = {"config": config, "registry": {"assets": {}}}
    objects = {}
    if media:
        payload = b"source media bytes"
        media_digest = hashlib.sha256(payload).hexdigest()
        objects[media_digest] = payload
        snapshot["registry"]["assets"]["image"] = {"object_id": "sha256:" + media_digest, "digest": media_digest, "type": "image/png"}
    if capture:
        snapshot["metadata"] = {"capture_authority": authority}
    else:
        authority["dependency_digests"] = [
            {"dependency_kind": "shot", "dependency_id": "shot-1", "content_digest": "sha256:" + "b" * 64},
            {"dependency_kind": "internal", "dependency_id": "internal-1", "content_digest": "sha256:" + "c" * 64}]
    ref = {"project_id": "project", "timeline_id": "timeline", "revision_id": "old-parent",
           "revision_digest": authority["parent_content_digest"], "source_kind": "revision"}
    if working_copy:
        assert capture
        marker = {"candidate_parent_revision_id": "old-parent", "candidate_digest": "sha256:" + "d" * 64,
                  "publication_digest": "sha256:" + "e" * 64,
                  "base_parent": {"revision_id": "base", "content_digest": "sha256:" + "f" * 64}}
        snapshot["metadata"]["working_copy"] = marker
        ref.update(source_kind="working_copy", revision_digest=marker["candidate_digest"], working_copy={
            "candidate_digest": marker["candidate_digest"], "publication_digest": marker["publication_digest"],
            "base_revision_id": "base", "base_revision_digest": marker["base_parent"]["content_digest"]})
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    objects[digest] = payload
    ref["digest"] = "sha256:" + digest
    if not capture:
        authority["snapshot_digest"] = ref["digest"]
    inputs = {"timeline_snapshot_ref": ref, "timeline_authority": authority}
    spec = {"input_object_ids": list(objects), "spec": {"inputs": inputs}}
    if capture:
        inputs.clear()
        inputs["filmstrip_authority"] = json.dumps({"capture_snapshot_ref": ref})
        spec["authority_context"] = {"capture_snapshot_ref": ref}
    host = GenericPackHost(pack_roots=[tmp_path], client=SimpleNamespace(get_object=lambda key: objects[key]))
    return host, spec, snapshot, ref


def materialize(tmp_path, monkeypatch, **kwargs):
    host, spec, snapshot, ref = admitted(tmp_path, **kwargs)
    values = host._materialize_inputs(spec, tmp_path / "attempt")
    monkeypatch.setenv(SOURCE_ENV, values.timeline_source._anchor)
    return host, spec, snapshot, ref, values


def request(values, **kwargs):
    return RenderRequest(
        schema_version=1,
        timeline_path=values["timeline"],
        assets_registry_path=values["assets_registry"],
        output_name="result.mp4",
        **kwargs,
    )


def test_render_request_keeps_verified_source_private_through_backend_projection(
    tmp_path, monkeypatch,
):
    _, _, _, _, values = materialize(tmp_path, monkeypatch)
    source = values.timeline_source
    parsed = request(values)

    assert parsed.verified_source == source
    assert parsed.for_backend("rendering.remotion").verified_source == source
    assert "verified_source" not in parsed.to_dict()


def test_render_request_rejects_reserved_backend_source_config(tmp_path, monkeypatch):
    _, _, _, _, values = materialize(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="host-owned"):
        request(values, backend_config={"rendering.remotion": {"verified_source": {}}})


def test_render_service_refuses_lost_verified_source_authority(tmp_path, monkeypatch):
    _, _, _, _, values = materialize(tmp_path, monkeypatch)
    parsed = request(values)
    monkeypatch.delenv(SOURCE_ENV)

    with pytest.raises(RendererException, match="authority is unavailable"):
        RenderService().render(parsed, out_path=tmp_path / "result.mp4")


@pytest.mark.parametrize("key", ["verified_source", "timeline_source", SOURCE_ENV])
@pytest.mark.parametrize("port", ["inputs", "params", "backend_config"])
def test_host_rejects_reserved_source_ports(tmp_path, port, key):
    host, spec, _, _ = admitted(tmp_path)
    if port == "backend_config":
        spec["spec"]["inputs"][port] = {"rendering.remotion": {key: {"canonical": True}}}
    else:
        spec["spec"].setdefault(port, {})[key] = True
    with pytest.raises(HostError, match="host-owned"):
        host._materialize_inputs(spec, tmp_path / "attempt")
    assert not (tmp_path / "attempt").exists()


@pytest.mark.parametrize("name", ["timeline", "assets_registry", "timeline_snapshot"])
def test_host_refuses_caller_replacement_of_verified_source(tmp_path, name):
    host, spec, _, _ = admitted(tmp_path)
    spec["spec"]["inputs"][name] = {"config": {}} if name == "timeline_snapshot" else "caller.json"
    with pytest.raises(HostError, match="caller replaced"):
        host._materialize_inputs(spec, tmp_path / "attempt")


@pytest.mark.parametrize("field,value", [("timeline_id", "foreign"), ("parent_revision_id", "newer"),
                                         ("parent_content_digest", "sha256:" + "0" * 64)])
def test_forged_authority_cannot_replace_admitted_ref(tmp_path, field, value):
    host, spec, _, _ = admitted(tmp_path)
    spec["spec"]["inputs"]["timeline_authority"][field] = value
    with pytest.raises(HostError, match="differs"):
        host._materialize_inputs(spec, tmp_path / "attempt")


def test_missing_authority_fails_closed(tmp_path):
    host, spec, _, _ = admitted(tmp_path)
    del spec["spec"]["inputs"]["timeline_authority"]
    with pytest.raises(HostError, match="requires admitted authority"):
        host._materialize_inputs(spec, tmp_path / "attempt")


@pytest.mark.parametrize("case,reason", [
    ("reserved", "verified timeline source is host-owned"),
    ("reference", "timeline snapshot reference differs from admitted context"),
    ("capture", "capture snapshot differs from admitted context"),
    ("authority", "verified source requires admitted authority and snapshot"),
    ("timeline", "verified source prepared inputs cannot be caller replaced"),
    ("assets_registry", "verified source prepared inputs cannot be caller replaced"),
    ("snapshot", "verified source snapshot cannot be caller replaced"),
    ("project_id", "verified source project_id differs from admitted authority"),
    ("timeline_id", "verified source timeline_id differs from admitted authority"),
    ("revision_id", "verified source revision_id differs from admitted authority"),
    ("revision_digest", "verified source requires an admitted revision digest"),
    ("content_digest", "verified source revision digest differs"),
    ("snapshot_digest", "verified source snapshot digest differs from Runtime authority"),
    ("working_copy", "verified source working copy differs"),
])
def test_host_preserves_literal_source_refusals(tmp_path, case, reason):
    host, spec, snapshot, ref = admitted(tmp_path, capture=case in {"capture", "working_copy"},
                                         working_copy=case == "working_copy")
    inputs = spec["spec"]["inputs"]
    if case == "reserved":
        spec["spec"][SOURCE_ENV] = "caller"
        # Reserved-key refusal precedes parameter validation and any fetch.
        spec["spec"]["params"] = "invalid"
        host.client.get_object = lambda key: pytest.fail("reserved source reached CAS")
    elif case == "reference":
        spec["spec"]["timeline_snapshot_ref"] = {**ref, "revision_id": "other"}
    elif case == "capture":
        inputs["timeline_snapshot"] = snapshot
        inputs["filmstrip_authority"] = json.dumps({"capture_snapshot": {}})
    elif case == "authority":
        inputs.pop("timeline_authority")
    elif case in {"timeline", "assets_registry"}:
        inputs[case] = "caller.json"
    elif case == "snapshot":
        spec["spec"]["timeline_snapshot"] = snapshot
    elif case in {"project_id", "timeline_id", "revision_id"}:
        inputs["timeline_authority"][{"revision_id": "parent_revision_id"}.get(case, case)] = "other"
    elif case == "revision_digest":
        ref["revision_digest"] = "invalid"
    elif case == "content_digest":
        inputs["timeline_authority"]["parent_content_digest"] = "sha256:" + "0" * 64
    elif case == "snapshot_digest":
        inputs["timeline_authority"]["snapshot_digest"] = "sha256:" + "0" * 64
    elif case == "working_copy":
        ref["working_copy"] = {}
        inputs["filmstrip_authority"] = json.dumps({"capture_snapshot_ref": ref})
    original = deepcopy(spec)
    with pytest.raises(HostError) as error:
        host._materialize_inputs(spec, tmp_path / "attempt", task_param_ports=() if case == "reserved" else None)
    assert str(error.value) == reason
    assert spec == original
    assert not (tmp_path / "attempt/inputs/verified-timeline-source.json").exists()


@pytest.mark.parametrize("capture,working_copy", [(False, False), (True, False), (True, True)])
def test_host_does_not_mutate_admitted_dictionaries(tmp_path, capture, working_copy):
    host, spec, snapshot, ref = admitted(tmp_path, capture=capture, working_copy=working_copy, media=True)
    original = deepcopy((spec, snapshot, ref))
    host._materialize_inputs(spec, tmp_path / "attempt")
    assert (spec, snapshot, ref) == original


def test_non_timeline_inputs_remain_ordinary_task_data(tmp_path):
    spec = {"spec": {"params": {"prompt": "hello", "options": {"nested": [1, True]}}}}
    original = deepcopy(spec)
    host = GenericPackHost(pack_roots=[tmp_path], client=SimpleNamespace())
    values = host._materialize_inputs(spec, tmp_path / "attempt", task_param_ports=("prompt", "options"))
    assert values == {"prompt": "hello", "options": {"nested": [1, True]}, "params": spec["spec"]["params"]}
    assert values.timeline_source is None
    assert spec == original
    assert not any(path.is_file() for path in (tmp_path / "attempt").rglob("*"))


def test_capture_keeps_existing_json_error_type(tmp_path):
    host, spec, snapshot, _ = admitted(tmp_path, capture=True)
    spec["spec"]["inputs"].update(timeline_snapshot=snapshot, filmstrip_authority="{")
    with pytest.raises(json.JSONDecodeError):
        host._materialize_inputs(spec, tmp_path / "attempt")


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("ambient", [False, True])
def test_source_handoff_changes_only_the_reserved_environment_key(tmp_path, verified, ambient):
    host, spec, _, _ = admitted(tmp_path)
    values = host._materialize_inputs(spec, tmp_path / "attempt")
    inputs = values if verified else dict(values)
    original = deepcopy(inputs)
    env = {"PYTHONPATH": "pack-path", "PROVIDER_TOKEN": "provider-value"}
    if ambient:
        env[SOURCE_ENV] = "caller-forgery"
    expected = {"PYTHONPATH": "pack-path", "PROVIDER_TOKEN": "provider-value"}
    if verified:
        expected[SOURCE_ENV] = values.timeline_source._anchor
    PreparedTimelineInputs.handoff_source(inputs, env)
    assert env == expected
    assert inputs == original


def test_source_handoff_clears_ambient_authority_before_refusing_changed_bytes(tmp_path):
    host, spec, _, _ = admitted(tmp_path)
    values = host._materialize_inputs(spec, tmp_path / "attempt")
    Path(values["timeline"]).write_text("{}")
    env = {SOURCE_ENV: "caller-forgery", "PYTHONPATH": "pack-path"}
    with pytest.raises(ValueError, match="^verified source prepared timeline bytes differ$"):
        PreparedTimelineInputs.handoff_source(values, env)
    assert env == {"PYTHONPATH": "pack-path"}



@pytest.mark.parametrize("capture,working_copy", [(False, False), (True, False), (True, True)])
def test_exact_admitted_source_and_materialized_pair(tmp_path, monkeypatch, capture, working_copy):
    _, _, snapshot, ref, values = materialize(
        tmp_path, monkeypatch, capture=capture, working_copy=working_copy, media=True)
    source = inherited_source(values["timeline"], values["assets_registry"])
    assert source == values.timeline_source
    binding = source.read()
    assert binding["ref"] == ref
    assert binding["snapshot"] == snapshot
    assert binding["authority"]["expansion"]["shots"]
    assert binding["authority"]["expansion"]["children"]
    assert not {"verified_source", "timeline_source", SOURCE_ENV}.intersection(values)
    registry = json.loads(Path(values["assets_registry"]).read_text())
    assert registry != snapshot["registry"]
    assert Path(registry["assets"]["image"]["file"]).read_bytes() == b"source media bytes"
    source.validate_snapshot(snapshot)


@pytest.mark.parametrize("path_key", ["timeline", "assets_registry"])
def test_prepared_source_refuses_equal_json_at_another_path(tmp_path, monkeypatch, path_key):
    _, _, _, _, values = materialize(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(Path(values[path_key]).read_bytes())
    values[path_key] = str(replacement)
    with pytest.raises(ValueError, match="path differs"):
        inherited_source(values["timeline"], values["assets_registry"])


@pytest.mark.parametrize("which", ["timeline", "assets_registry", "binding"])
def test_prepared_source_refuses_replaced_bytes(tmp_path, monkeypatch, which):
    _, _, _, _, values = materialize(tmp_path, monkeypatch)
    path = Path(json.loads(values.timeline_source._anchor)[0]) if which == "binding" else Path(values[which])
    path.write_text('{}')
    with pytest.raises(ValueError, match="(bytes differ|binding changed)"):
        inherited_source(values["timeline"], values["assets_registry"])


def test_typed_source_cannot_be_constructed_from_caller_json():
    with pytest.raises(ValueError, match="host-owned"):
        VerifiedTimelineSource('["caller.json", "sha256:forged"]')


@pytest.mark.parametrize("key,value", [("hold", True), ("duration", 2.0)])
def test_capture_binding_preserves_json_numeric_identity(tmp_path, monkeypatch, key, value):
    _, _, snapshot, _, values = materialize(
        tmp_path, monkeypatch, capture=True, config={"tracks": [], "clips": [], "hold": 1, "duration": 2})
    forged = deepcopy(snapshot)
    forged["config"][key] = value
    assert forged["config"] == snapshot["config"]
    with pytest.raises(ValueError, match="capture config differs"):
        values.timeline_source.validate_snapshot(forged)


@pytest.mark.parametrize("key", ["config", "registry", "metadata"])
def test_capture_replacement_refuses_at_same_binding(tmp_path, monkeypatch, key):
    _, _, snapshot, _, values = materialize(tmp_path, monkeypatch, capture=True)
    forged = deepcopy(snapshot)
    forged[key] = {"forged": True}
    with pytest.raises(ValueError, match="capture .*differs"):
        values.timeline_source.validate_snapshot(forged)


def test_runtime_snapshot_digest_cannot_be_replaced(tmp_path):
    host, spec, _, _ = admitted(tmp_path)
    spec['spec']['inputs']['timeline_authority']['snapshot_digest'] = 'sha256:' + '0' * 64
    with pytest.raises(HostError, match='snapshot digest differs'):
        host._materialize_inputs(spec, tmp_path / 'attempt')


def test_capture_working_copy_reference_cannot_be_replaced(tmp_path):
    host, spec, _, _ = admitted(tmp_path, capture=True, working_copy=True)
    context = spec['authority_context']
    context['capture_snapshot_ref'] = {**context['capture_snapshot_ref'], 'working_copy': {'candidate_digest': 'forged'}}
    with pytest.raises(HostError, match='differs from admitted context'):
        host._materialize_inputs(spec, tmp_path / 'attempt')


def test_host_command_receives_only_its_prepared_source(tmp_path, monkeypatch):
    from tests.test_generic_host import _write_manifest

    host, spec, snapshot, ref = admitted(tmp_path)
    values = host._materialize_inputs(spec, tmp_path / 'attempt')
    pack = tmp_path / 'probe'
    manifest_path = _write_manifest(pack)
    script = pack / 'probe.py'
    script.write_text('''
import json, sys
from pathlib import Path
from astrid.core.rendering.verified_source import inherited_source
source = inherited_source(sys.argv[1], sys.argv[2])
Path(sys.argv[3]).write_text(json.dumps(source.read()))
''')
    manifest = json.loads(manifest_path.read_text())
    manifest['command']['argv'] = ['{python_exec}', str(script), '{timeline}', '{assets_registry}', '{out}/answer.txt']
    manifest['inputs'] = [{'name': name, 'type': 'file', 'required': True}
                          for name in ('timeline', 'assets_registry')]
    manifest_path.write_text(json.dumps(manifest))
    host.discover()
    output = tmp_path / 'attempt/outputs'
    output.mkdir()
    monkeypatch.setenv(SOURCE_ENV, 'ambient-forgery')
    host._run_command_definition(host.capabilities['test.echo'], values, output, tmp_path / 'attempt',
                                 allow_runtime_connection=False)
    binding = json.loads((output / 'answer.txt').read_text())
    assert binding['ref'] == ref
    assert binding['snapshot'] == snapshot
