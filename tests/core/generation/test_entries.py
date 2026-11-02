from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, entries, listing
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource


def test_output_kind_ambiguity():
    workflow = VibeWorkflow('test', WorkflowSource('test'))
    workflow.nodes['1'] = VibeNode('1', 'SaveImage')
    assert entries.infer_kind(workflow) == 'image'
    workflow.nodes['2'] = VibeNode('2', 'VHS_VideoCombine')
    with pytest.raises(ValueError, match='ambiguous'):
        entries.infer_kind(workflow)


def test_explicit_node_binding_uses_existing_input_contract():
    workflow = VibeWorkflow('test', WorkflowSource('test'))
    workflow.nodes['1'] = VibeNode('1', 'EmptyLatentImage', inputs={'width': 512})
    assert entries._resolve_binding(workflow, 'width', {'node': '1', 'field': 'width'}) == 'width'
    workflow.set_input('width', 768)
    assert workflow.nodes['1'].inputs['width'] == 768


def test_explicit_media_node_binding_preserves_staging_semantics():
    workflow = VibeWorkflow("test", WorkflowSource("test"))
    workflow.nodes["1"] = VibeNode("1", "LoadImage", inputs={"image": "placeholder.png"})
    assert entries._resolve_binding(workflow, "image_ref", {"node": "1", "field": "image"}) == "image_ref"
    assert workflow.inputs["image_ref"].media_semantics == "image"


def test_ready_projection_uses_index_and_coverage():
    rows = entries.ready_entries()
    z = next(row for row in rows if row['id'] == 'image/z_image_turbo')
    assert z['kind'] == 'image' and z['modes'] == ['t2i']
    unknown = [row for row in rows if row['ambiguity']]
    assert unknown and all(row['kind'] is None or len(row['modes']) != 1 for row in unknown)


def test_gateway_workflow_dispatch(monkeypatch):
    from astrid import console
    calls = []
    monkeypatch.setattr(cli, 'main', lambda args: calls.append(args) or 0)
    assert console.main(['generate', 'workflow', 'graph.json', '--dry-run']) == 0
    assert calls == [['workflow', 'graph.json', '--dry-run']]


def test_explicit_entry_plan_is_visible(loopback_comfy, tmp_path, monkeypatch, capsys):
    from vibecomfy.entries import import_as_entry
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle
    source = tmp_path / "ui.json"
    source.write_text(json.dumps(load_bundle(workflow_from_ready("image/z_image_turbo")).materialize_ui()))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    import_as_entry(source, "mine")
    assert cli.main(["image", "hello", "--entry", "mine", "--target", loopback_comfy.url, "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["entry"]["id"] == "mine" and plan["entry"]["reason"] == "explicitly selected workflow entry"
    assert plan["entry"]["bindings"]["prompt"] == "hello"


def test_default_plan_selects_highest_ranked_ready_entry(loopback_comfy, monkeypatch, capsys):
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    assert cli.main(["image", "hello", "--target", loopback_comfy.url, "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["entry"]["id"] == "z-image"
    assert "highest-ranked ready" in plan["entry"]["reason"]
    assert plan["target"]["endpoint"] == loopback_comfy.url


def test_workflow_audio_runs_with_inferred_kind_without_forcing_audio_mode(monkeypatch, capsys, tmp_path):
    import astrid.sdk.generation as sdk_generation
    from vibecomfy.workflow_bundle import emit_bundle
    workflow = VibeWorkflow("audio", WorkflowSource("audio"))
    workflow.nodes["1"] = VibeNode("1", "SaveAudio", uid="save")
    (tmp_path / 'source.json').write_text(json.dumps({'nodes': [
        {'id': 1, 'type': 'SaveAudio', 'inputs': [], 'widgets_values': []}], 'links': []}))
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    monkeypatch.setattr(sdk_generation, "_load_model_registry", lambda **kwargs: object())
    calls = []
    monkeypatch.setattr(sdk_generation.GenerationFacade, '_generate_on_target',
        lambda self, **kwargs: calls.append(kwargs) or {"kind": kwargs["modality"]})
    assert cli.main(["workflow", str(bundle.python_path), "--dry-run"]) == 0
    assert calls[0]["modality"] == "audio" and calls[0]["mode"] == "workflow"
    assert json.loads(capsys.readouterr().out)["kind"] == "audio"

    # Keep the original UI-JSON entry point alongside int/r1's canonical bundle.
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'audio.json').write_bytes((tmp_path / 'source.json').read_bytes())
    assert cli.main(["workflow", "audio.json", "--dry-run"]) == 0
    assert calls[1]["modality"] == "audio" and calls[1]["mode"] == "workflow"
    assert json.loads(capsys.readouterr().out)["kind"] == "audio"


from tests.core.generation.test_generate_target_journey import loopback_comfy, PACK


def test_catalog_description_has_same_unqualified_reason_and_fix_as_listing(monkeypatch):
    from astrid.core.model_catalog.registry import ModelRegistry
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'action_entries', lambda *args: [])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(listing, 'discover', lambda: SimpleNamespace(actions=[]))
    description = entries.describe_entry('wan-2.2', registry=ModelRegistry.load_default())
    rows = [row for row in listing.catalog_rows() if row['id'] == 'wan-2.2']
    assert rows
    for key in ('availability', 'reason', 'fix'):
        assert description[key] == rows[0][key]
    assert description['availability'] == 'unavailable'
    assert description['reason'] and description['fix']
    assert all(row['availability'] == 'unavailable' and row['fix']
               for row in description['mode_descriptions'].values())


@pytest.mark.parametrize('qualified', [False, True])
@pytest.mark.parametrize('source', ['action', 'ready-template', 'user'])
def test_description_projects_qualification_without_gating_user_workflows(monkeypatch, source, qualified):
    from astrid.core.generation import availability
    from astrid.core.model_catalog.registry import ModelRegistry
    record = {'available': qualified, 'error': {'code': 'generation_route_unqualified',
        'message': 'synthetic route qualification missing'}}
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {
        'actions': {'fake.animate': record}, 'models': {'fake': record},
        'template_models': {'video/wan': 'fake'}})
    entry = {'id': 'video/wan_fake', 'kind': 'video', 'modes': ['i2v'],
             'inputs': [], 'bundle': 'unused.py', 'source': source}
    if source == 'action':
        entry['action'] = 'fake.animate'
    elif source == 'user':
        entry['source'] = 'ready-template'  # Imported metadata does not confer bundled provenance.
    monkeypatch.setattr(entries, 'action_entries', lambda: [entry] if source == 'action' else [])
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry] if source == 'user' else [])
    monkeypatch.setattr(entries, '_ready_entries_for', lambda _id: [entry])
    monkeypatch.setattr(entries, '_ready_ids', lambda: set())
    monkeypatch.setattr(entries, '_load_entry_workflow', lambda _: object())
    monkeypatch.setattr(entries, '_workflow_inputs', lambda *a, **k: [])
    description = entries.describe_entry(entry['id'], registry=ModelRegistry([]))
    expected_available = qualified or source == 'user'
    assert description['availability'] == ('available' if expected_available else 'unavailable')
    if not expected_available:
        assert description['reason'] == record['error']['message']
        assert description['fix']


def test_import_list_and_generate_entry(loopback_comfy, tmp_path, monkeypatch, capsys, real_runtime):
    from concurrent.futures import ThreadPoolExecutor
    from astrid import console
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.sdk.client import AstridClient
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle
    from vibecomfy.entries import import_as_entry
    import astrid.core.execution.generic_host as host_module
    import vibecomfy

    source = tmp_path / 'ui.json'
    source.write_text(json.dumps(load_bundle(workflow_from_ready('image/z_image_turbo')).materialize_ui()))
    monkeypatch.setenv('VIBECOMFY_ENTRIES_DIR', str(tmp_path / 'entries'))
    entry = import_as_entry(source, 'my-style')
    assert entry['kind'] == 'image' and 'prompt' in entry['binds']
    from astrid.core.generation.backends.comfy_target import ComfyTarget
    monkeypatch.setattr(listing, '_targets', lambda: [(ComfyTarget(loopback_comfy.url, 'attached'), loopback_comfy.schema)])
    assert console.main(['generate', 'list', '--kind', 'image', '--json']) == 0
    assert any(row['id'] == 'my-style' and row['source'] == 'user-entry' and row['ready'] for row in json.loads(capsys.readouterr().out))
    daemon = real_runtime()
    client = AstridClient.open(endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm['id'], actor_id='owner', client_name='g30-entry',
        client_version='test', protocol_version='workspace.v1')
    project = client.projects.create(slug='g30', name='G30', idempotency_key='g30-project')
    assert project.ok
    dependency_root = str(Path(vibecomfy.__file__).resolve().parents[1])
    monkeypatch.setattr(host_module, '_dependency_pythonpath', lambda: (dependency_root,))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(client))
    invoke_result = client.invoke_result
    def bounded(*args, **kwargs):
        kwargs.update(timeout_seconds=45.0, poll_seconds=0.02)
        return invoke_result(*args, **kwargs)
    monkeypatch.setattr(client, 'invoke_result', bounded)
    host = GenericPackHost(pack_roots=[PACK], attempt_root=tmp_path / 'attempt',
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token))
    host.discover()
    host.register()
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            settled = worker.submit(host.run, poll_seconds=0.02, max_tasks=1)
            try:
                code = console.main(['generate', 'image', 'entry test prompt', '--entry', 'my-style',
                    '--target', loopback_comfy.url, '--project', project.data['project_id'], '--json'])
            finally:
                host.shutdown()
            results = settled.result(timeout=15)
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        assert len(results) == 1 and results[0].state == 'succeeded'
        assert any(n['class_type'] == 'CLIPTextEncode' and n['inputs'].get('text') == 'entry test prompt'
                   for n in loopback_comfy.prompts[0].values())
    finally:
        host.shutdown()
