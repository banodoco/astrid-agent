"""Admission and submission preserve a single built workflow."""

from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade
from tests.sdk.test_generation_target import (
    attached_without_managed, engine_configuration, install_target, network,
    observation_configuration, storage_client, target_info, terminal_result,
)


def test_explicit_workflow_builds_once_and_submits_approved_graph_per_command(network, monkeypatch, tmp_path):
    from astrid.core.generation import entries
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.security.provenance import Provenance
    import vibecomfy.workflow_bundle as bundles

    client, _ = install_target(network, target_info(vaes=('ae.safetensors',)))
    attached_without_managed(monkeypatch)
    path = tmp_path / 'authored.py'
    bundles.emit_bundle(workflow_from_ready('image/z_image_turbo'), path, {'operation': 'authored'})
    source_bytes = b'{"original": "workflow"}'
    path.with_name('source.json').write_bytes(source_bytes)
    runtime, imported = storage_client()
    builds = []
    emissions = []
    approvals = []
    submitted = []
    prepare = entries.prepare
    emit = bundles.emit_bundle

    def record_prepare(*args, **kwargs):
        builds.append(1)
        return prepare(*args, **kwargs)

    def record_emit(*args, **kwargs):
        bundle = emit(*args, **kwargs)
        emissions.append(bundle)
        if len(emissions) == 1:
            compile_bundle = type(bundle).compile

            def record_compile(self, *args, **kwargs):
                result = compile_bundle(self, *args, **kwargs)
                if any(self is emitted for emitted in emissions):
                    approvals.append((result.api_projection, kwargs['schema_provider']))
                return result

            monkeypatch.setattr(type(bundle), 'compile', record_compile)
        return bundle

    def invoke_workflow(self, **kwargs):
        assert len(approvals) == len(submitted) + 1
        graph, schema = approvals[-1]
        bundle = bundles.load_bundle(imported[kwargs['python']['digest']], trust=Provenance.USER_CONFIRMED)
        bundle.require_canonical_authority('test generation admission')
        assert bundle.compile(schema_provider=schema).api_projection == graph
        assert imported[kwargs['companion']['digest']].is_file()
        assert runtime.media.snapshots[kwargs['source']['digest']] == source_bytes
        submitted.append(graph)
        return terminal_result(tmp_path)

    monkeypatch.setattr(entries, 'prepare', record_prepare)
    monkeypatch.setattr(bundles, 'emit_bundle', record_emit)
    monkeypatch.setattr(GenerationFacade, 'workflow', invoke_workflow)
    entry = {'id': 'workflow', 'kind': 'image', 'modes': ['t2i'], 'bundle': str(path)}
    for index, prompt in enumerate(('silver boat', 'gold boat'), start=1):
        result = GenerationFacade()._generate_on_target(
            registry=ModelRegistry([]), model=None, mode='t2i', modality='image',
            placement=client.target.url, execution=None, inputs={'prompt': prompt, 'seed': index},
            dry_run=False, workflow_entry=entry, client=runtime, project='demo')
        assert result.ok
        assert len(builds) == len(emissions) == len(approvals) == len(submitted) == index
        by_class = {node['class_type']: node['inputs'] for node in submitted[-1].values()}
        sampler = by_class['KSampler']
        assert submitted[-1][sampler['positive'][0]]['inputs']['text'] == prompt
        assert sampler['seed'] == index
    assert submitted[0] != submitted[1]
