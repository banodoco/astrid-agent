"""Independent default population and parent semantics, derived from real packs."""
from pathlib import Path

from astrid.core.element.registry import load_default_registry as load_elements
from astrid.core.execution.executor.registry import load_default_registry as load_executors
from astrid.core.execution.orchestrator.registry import load_default_registry as load_orchestrators
from astrid.core.pack import discover_packs
from astrid.core.pack.manifest import load_manifest_mapping


def test_default_population_preserves_the_declared_parent_contract():
    packs = discover_packs()
    executors = load_executors()
    parents = load_orchestrators(executor_registry=executors)
    elements = load_elements()
    assert (len(packs), len(executors.list()), len(parents.list()), len(elements.list())) == (30, 107, 1, 44)
    assert {parent.id for parent in parents.list()} == {'video_editing.hype'}
    assert 'video_editing.hype' not in {executor.id for executor in executors.list()}
    owner = next(pack for pack in packs if pack.id == 'video_editing')
    action = load_manifest_mapping(Path(owner.manifest_path), manifest_kind='pack')['actions']['hype']
    assert action['metadata']['local_orchestrator'] is True
    expected_children = {
        'editorial.transcribe', 'editorial.scenes', 'editorial.quality_zones',
        'editorial.shots', 'editorial.triage', 'understanding.scene_describe',
        'editorial.quote_scout', 'training.pool_build', 'training.pool_merge',
        'editorial.arrange', 'video_editing.cut', 'editorial.refine',
        'rendering.render', 'editorial.editor_review', 'editorial.validate',
    }
    assert set(action['graph']['depends_on']) == expected_children
    assert set(action['graph']['child_actions']) == expected_children
    parent = parents.get('video_editing.hype')
    assert set(parent.child_executors) == expected_children
    assert parent.child_orchestrators == ()
    assert parent.runtime.kind == 'command'
    assert parent.runtime.command.argv == tuple(action['invocation']['command']['argv'])
