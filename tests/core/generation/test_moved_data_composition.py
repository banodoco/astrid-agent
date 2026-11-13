"""Real installed-pack composition coverage for the public generation listing."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
import pytest

from astrid.core.execution.executor.registry import load_default_registry as load_executors
from astrid.core.generation import listing
from astrid.core.generation.availability import availability_declaration
from astrid.core.generation.entries import action_entries
from astrid.sdk.invocation import discover
from scripts import gen_capability_index


PACK_ID = "moved_fixture"
ACTION_ID = f"{PACK_ID}.generate_image"
ENTRY_ID = f"{PACK_ID}.image"
POLICY_REASON = "fixture route is awaiting qualification"
POLICY_FIX = "qualify the fixture route before use"


def _policy(available: bool, message: str = POLICY_REASON) -> dict:
    return {
        "available": available,
        "error": {
            "code": "generation_route_unqualified",
            "message": message,
            "fix": POLICY_FIX,
        },
    }


def _manifest(pack_id: str, *, schema_version: int = 3, availability=None) -> dict:
    manifest = {
        "schema_version": schema_version,
        "id": pack_id,
        "name": "Moved Data Fixture",
        "version": "1.0.0",
        "pack_api": "2.0",
        "status": "active",
        "domain": "media",
        "stability": "experimental",
        "support": "core",
        "visibility": "visible",
        "description": "Temporary installed-only generation fixture.",
        "capabilities": [],
        "actions": {
            "generate_image": {
                "name": "Fixture image generator",
                "version": "1.0.0",
                "short_description": "Installed fixture | image generation",
                "description": "Installed-only fixture action for generation catalog composition.",
                "invocation": {"kind": "command", "command": {"argv": ["python", "-c", "pass"]}},
                "inputs": [
                    {
                        "name": "prompt",
                        "type": "string",
                        "required": True,
                        "description": "Text prompt for the fixture image.",
                    }
                ],
                "outputs": [],
                "isolation": {"mode": "subprocess", "network": False, "requirements": ["fixture-sandbox"]},
                "metadata": {
                    "adapter_family": "cpu",
                    "resource_keys": ["cpu"],
                    "generation_entry": {
                        "id": f"{pack_id}.image",
                        "kind": "image",
                        "modes": ["t2i"],
                        "inputs": ["prompt"],
                        "requirements": ["fixture-model-v1", "cpu"],
                    },
                },
            }
        },
    }
    if availability is not None:
        manifest["extensions"] = {
            "astrid": {
                "generation_availability": {
                    "actions": {f"{pack_id}.generate_image": availability}
                }
            }
        }
    return manifest


def _write_pack(root: Path, pack_id: str, *, schema_version: int = 3, availability=None) -> Path:
    pack_root = root / pack_id
    pack_root.mkdir(parents=True, exist_ok=True)
    (pack_root / "pack.yaml").write_text(
        yaml.safe_dump(_manifest(pack_id, schema_version=schema_version, availability=availability), sort_keys=False),
        encoding="utf-8",
    )
    return pack_root


def _fixture_rows(rows: list[dict], entry_id: str = ENTRY_ID) -> list[dict]:
    return [row for row in rows if row["id"] == entry_id]


# Measured 172 s on a hosted run (CI 43348849041); the suite default is 120 s.
@pytest.mark.timeout(300)
def test_real_selected_manifest_composes_through_generation_cli(monkeypatch, tmp_path, capsys):
    selected_root = tmp_path / "selected-packs"
    unselected_root = tmp_path / "unselected-packs"
    pack_root = _write_pack(selected_root, PACK_ID, availability=_policy(False))
    _write_pack(unselected_root, "unselected_fixture", availability=_policy(False))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(selected_root))
    monkeypatch.setenv("ASTRID_HOME", str(tmp_path / "astrid-home"))

    # Observe real filesystem reads; unselected manifests and ambient Python
    # package trees cannot participate in discovery or affect the public rows.
    ambient_root = tmp_path / 'ambient-pythonpath'
    _write_pack(ambient_root / 'astrid/packs', PACK_ID, schema_version=999)
    monkeypatch.syspath_prepend(str(ambient_root))
    original_open = Path.open

    def observed_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if 'r' in mode:
            assert not path.resolve().is_relative_to(unselected_root)
            assert not path.resolve().is_relative_to(ambient_root)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed_open)

    # Real manifest parsing, ordered discovery, executor registration, and SDK discovery.
    executors = load_executors()
    selected_executors = [
        item for item in executors.list()
        if Path(item.metadata["pack_root"]).resolve() == pack_root.resolve()
    ]
    assert [item.id for item in selected_executors] == [ACTION_ID]
    assert not any(item.id.startswith("unselected_fixture.") for item in executors.list())

    discovery = discover()
    discovered_action_ids = {action.id for action in discovery.actions}
    assert ACTION_ID in discovered_action_ids
    assert not any(identifier.startswith("unselected_fixture.") for identifier in discovered_action_ids)
    projected_entries = action_entries(discovery)
    fixture_entry = [entry for entry in projected_entries if entry["id"] == ENTRY_ID]
    assert fixture_entry == [{
        "id": ENTRY_ID,
        "kind": "image",
        "modes": ["t2i"],
        "action": ACTION_ID,
        "inputs": [{
            "name": "prompt",
            "type": "string",
            "required": True,
            "description": "Text prompt for the fixture image.",
        }],
        "requirements": ["fixture-model-v1", "cpu"],
        "isolation_requirements": ["fixture-sandbox"],
    }]

    # The shared catalog and public command run without stubbing discovery, registries,
    # catalog_rows, the generation-entry projection, or manifest parsing.
    catalog_fixture = _fixture_rows(listing.catalog_rows())
    assert catalog_fixture == [{
        "id": ENTRY_ID,
        "kind": "image",
        "mode": "t2i",
        "backend": "action",
        "template": ACTION_ID,
        "source": "action-entry",
        "availability": "unavailable",
        "reason": POLICY_REASON,
        "requirements": ["fixture-model-v1", "cpu"],
        "isolation_requirements": ["fixture-sandbox"],
        "fix": POLICY_FIX,
    }]

    # Empty target inventory is the only host-probing boundary isolated here.
    monkeypatch.setattr(listing, "_targets", lambda: [])
    from astrid import console

    assert console.main(["generate", "list", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert _fixture_rows(output) == [{
        "id": ENTRY_ID,
        "kind": "image",
        "mode": "t2i",
        "backend": "action",
        "template": ACTION_ID,
        "source": "action-entry",
        "availability": "unavailable",
        "reason": POLICY_REASON,
        "requirements": ["fixture-model-v1", "cpu"],
        "isolation_requirements": ["fixture-sandbox"],
        "fix": POLICY_FIX,
        "target": "declaration",
        "ready": False,
        "models_present": [],
    }]
    assert not any(row["id"].startswith("unselected_fixture.") for row in output)
    (unselected_root / 'unselected_fixture/pack.yaml').write_text('invalid: [changed\n')
    assert console.main(["generate", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == output

    # The installed action is available to real discovery but excluded from the bundled index.
    ambient_home = unselected_root / 'home'
    _write_pack(ambient_home / 'Astrid/packs', 'ambient_home_fixture', schema_version=999)
    with monkeypatch.context() as scoped:
        scoped.setenv('HOME', str(ambient_home))
        first_block = gen_capability_index.render_block()
        second_block = gen_capability_index.render_block()
        assert Path.home() == ambient_home
    assert first_block == second_block
    assert ACTION_ID not in first_block
    assert ENTRY_ID not in first_block
    assert "unselected_fixture" not in first_block
    assert gen_capability_index._row(ACTION_ID, "Installed fixture | image generation") == (
        f"| `{ACTION_ID}` | Installed fixture \\| image generation |"
    )

    # An invalid selected schema is quarantined and contributes no fixture identity.
    unsupported_root = tmp_path / "unsupported-packs"
    _write_pack(unsupported_root, "unsupported_fixture", schema_version=999, availability=_policy(False))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(unsupported_root))
    unsupported_executors = load_executors()
    assert not any(item.id.startswith("unsupported_fixture.") for item in unsupported_executors.list())
    unsupported_discovery = discover()
    assert not any(action.id.startswith("unsupported_fixture.") for action in unsupported_discovery.actions)
    assert not any(row["id"].startswith("unsupported_fixture.") for row in listing.catalog_rows())
    assert not any(
        action_id.startswith("unsupported_fixture.")
        for action_id in availability_declaration().get("actions", {})
    )
    assert console.main(["generate", "list", "--json"]) == 0
    unsupported_output = json.loads(capsys.readouterr().out)
    assert not any(row["id"].startswith("unsupported_fixture.") for row in unsupported_output)


def test_selected_generation_policy_refreshes_after_edit_and_removal(monkeypatch, tmp_path):
    selected_root = tmp_path / "policy-packs"
    pack_root = _write_pack(selected_root, "policy_fixture", availability=_policy(False, "initial policy"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(selected_root))
    monkeypatch.setenv("ASTRID_HOME", str(tmp_path / "astrid-home"))

    action_id = "policy_fixture.generate_image"
    assert availability_declaration()["actions"][action_id]["error"]["message"] == "initial policy"

    updated = _manifest("policy_fixture", availability=_policy(False, "updated policy"))
    (pack_root / "pack.yaml").write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
    assert availability_declaration()["actions"][action_id]["error"]["message"] == "updated policy"

    (pack_root / "pack.yaml").unlink()
    assert action_id not in availability_declaration()["actions"]


def test_non_python_workflow_uses_selected_template_policy(monkeypatch, tmp_path):
    import pytest
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.availability import require_bundled_workflow_available

    root = tmp_path / 'selected'
    pack = _write_pack(root, 'policy_fixture')
    data = _manifest('policy_fixture')
    data['extensions'] = {'astrid': {'generation_availability': {
        'models': {'fixture-model': _policy(False, 'selected model is blocked')},
        'template_models': {'fixture/workflow': 'fixture-model'},
    }}}
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    descriptor = tmp_path / 'workflow.json'
    descriptor.write_text(json.dumps({'template_id': 'fixture/workflow-v1', 'bindings': {}}))
    with pytest.raises(AstridError, match='selected model is blocked') as error:
        require_bundled_workflow_available(descriptor)
    assert error.value.code == 'generation_route_unqualified'
    assert error.value.fix == POLICY_FIX


def test_selected_policy_has_one_model_owner(monkeypatch, tmp_path):
    import pytest
    from astrid.core.pack import PackValidationError

    root = tmp_path / 'selected'
    for pack_id in ('first_owner', 'second_owner'):
        pack = _write_pack(root, pack_id)
        data = _manifest(pack_id)
        data['extensions'] = {'astrid': {'generation_availability': {
            'models': {'fixture-model': {'available': pack_id == 'second_owner'}},
        }}}
        if pack_id == 'first_owner':
            data['extensions']['astrid']['generation_availability']['models']['fixture-model'] = _policy(False)
        (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    with pytest.raises(PackValidationError, match='models.fixture-model has multiple owners: first_owner, second_owner'):
        availability_declaration()


def test_selected_registry_policy_cannot_read_same_id_unselected_manifest(monkeypatch, tmp_path):
    from astrid.core.execution.capability_ledger import _executor_inventory, load_capability_ledger

    root = tmp_path / 'selected'
    pack = _write_pack(root, 'policy_fixture')
    data = _manifest('policy_fixture')
    data['extensions'] = {'astrid': {'output_result_exemptions': {'actions': {
        'generate_image': {'reasons': ['no-artifact'], 'note': 'selected action owns this policy'},
    }}}}
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    project = tmp_path / 'project'
    stale = project / 'astrid/packs/policy_fixture'
    stale.mkdir(parents=True)
    # Any attempt to read this same-ID checkout manifest must fail canonical admission.
    (stale / 'pack.yaml').write_text('this: is not an admitted pack\n')
    matrix = project / 'config/astrid-beta-capabilities.json'
    matrix.parent.mkdir()
    models = project / 'astrid/core/model_catalog/models.yaml'
    models.parent.mkdir(parents=True)
    models.write_text('models: []\n')
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': []}))
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    registry = load_executors()
    assert registry.get('policy_fixture.generate_image').metadata['pack_root'] == str(pack)
    expected = {
        'id': 'policy_fixture.generate_image', 'kind': 'executor', 'result_contract': 'exempted',
        'disposition': 'historical', 'reason': 'selected action owns this policy',
        'reasons': ['no-artifact'], 'source': str(pack / 'pack.yaml'),
    }
    assert [row for row in _executor_inventory(project, executor_registry=registry)
            if row['id'] == expected['id']] == [expected]
    ledger = load_capability_ledger(matrix, executor_registry=registry)
    selected_rows = [row for row in ledger['sources']['executor_inventory'] if row['id'] == expected['id']]
    assert selected_rows == [{**expected, 'reason': expected['reason'],
                              'discovery_status': 'historical_only', 'executable': False}]
    scoped = load_capability_ledger(matrix, pack_roots=(pack,))
    assert [row['id'] for row in scoped['sources']['executor_inventory']
            if row['discovery_status'] != 'historical_only'] == []
    assert any(row['id'] == expected['id'] for row in scoped['sources']['executor_inventory'])


def test_selected_policy_rejects_cross_owner_template_prefix_override(monkeypatch, tmp_path):
    import pytest
    from astrid.core.pack import PackValidationError

    root = tmp_path / 'selected'
    for pack_id, prefix in [('first_owner', 'fixture/'), ('second_owner', 'fixture/nested/')]:
        pack = _write_pack(root, pack_id)
        data = _manifest(pack_id)
        data['extensions'] = {'astrid': {'generation_availability': {
            'models': {pack_id: _policy(False)},
            'template_models': {prefix: pack_id},
        }}}
        (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    with pytest.raises(PackValidationError, match='template families .* have different owners'):
        availability_declaration()


def test_selected_policy_snapshot_is_immutable_and_detached(monkeypatch, tmp_path):
    import pytest
    from astrid.core.pack import load_pack_manifest
    from astrid.core.generation.availability import template_availability

    root = tmp_path / 'selected'
    pack = _write_pack(root, 'policy_fixture')
    data = _manifest('policy_fixture', availability=_policy(False))
    data['extensions']['astrid']['generation_availability'].update({
        'models': {'fixture': _policy(False, 'snapshot decision')},
        'template_models': {'fixture/': 'fixture', 'fixture/nested/': 'fixture'},
    })
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    selected = load_pack_manifest(pack / 'pack.yaml')
    snapshot = availability_declaration(selected_packs=(selected,))
    with pytest.raises(TypeError):
        snapshot['models']['fixture']['available'] = True
    selected.extensions['astrid']['generation_availability']['models']['fixture']['available'] = True
    assert template_availability('fixture/nested/template', 't2i', 'local', declaration=snapshot)['available'] is False
    assert snapshot['actions']['policy_fixture.generate_image']['error']['fix'] == POLICY_FIX


def test_one_render_captures_policy_before_catalog_sources_change(monkeypatch, tmp_path, capsys):
    from astrid import console
    from astrid.core.generation import entries

    root = tmp_path / 'selected'
    pack = _write_pack(root, PACK_ID, availability=_policy(False, 'first decision'))
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'astrid-home'))
    original = entries.ready_entries

    def change_policy_during_render():
        data = _manifest(PACK_ID, availability=_policy(True))
        (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
        return original()

    # Instrument a real catalog source; no catalog/discovery rows are replaced.
    monkeypatch.setattr(entries, 'ready_entries', change_policy_during_render)
    monkeypatch.setattr(listing, '_targets', lambda: [])
    assert console.main(['generate', 'list', '--json']) == 0
    first = _fixture_rows(json.loads(capsys.readouterr().out))
    assert len(first) == 1 and first[0]['availability'] == 'unavailable'
    assert first[0]['reason'] == 'first decision' and first[0]['fix'] == POLICY_FIX
    assert console.main(['generate', 'list', '--json']) == 0
    second = _fixture_rows(json.loads(capsys.readouterr().out))
    assert len(second) == 1 and second[0]['availability'] == 'available'
    assert second[0]['backend'] == 'action'


@pytest.mark.parametrize("undeclared_python", [False, True])
def test_explicit_renderer_inventory_never_reads_ambient_or_unselected_bytes(monkeypatch, tmp_path, undeclared_python):
    from astrid.core.execution.capability_ledger import load_capability_ledger, capability_execution_admission

    project = tmp_path / 'project'
    matrix = project / 'config/astrid-beta-capabilities.json'
    matrix.parent.mkdir(parents=True)
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': []}))
    model_catalog = project / 'astrid/core/model_catalog/models.yaml'
    model_catalog.parent.mkdir(parents=True)
    model_catalog.write_text('models: []\n')
    root = tmp_path / 'selected'
    pack = root / 'render_fixture'
    pack.mkdir(parents=True)
    data = _manifest('render_fixture')
    data['actions'] = {}
    data['rendering'] = {'chosen': {'type': 'renderer', 'path': 'rendering/chosen.yaml'}}
    (pack / 'rendering').mkdir()
    renderer = pack / 'rendering/chosen.yaml'
    renderer.write_text('id: render_fixture.chosen\nrequired_binaries: [selected-renderer]\n')
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    forbidden = [tmp_path / 'ambient', project / 'astrid/packs', pack / 'unselected.yaml',
                 pack / 'executor.yaml', pack / 'unselected.py', pack / 'renderer.yaml']
    ambient = _write_pack(forbidden[0], 'ambient_fixture', schema_version=999)
    stale = forbidden[1] / 'rendering/rendering/renderers/stale'
    stale.mkdir(parents=True)
    (stale / 'renderer.yaml').write_text('id: unselected.stale\n')
    forbidden[2].write_text('id: unselected.payload\n')
    forbidden[3].write_text('id: unselected.executor\n')
    if undeclared_python:
        forbidden[4].write_text('from astrid.core import forbidden_import\n')
    forbidden[5].write_text('id: unselected.renderer\n')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(ambient))
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'empty-home'))
    original = Path.open
    reads = []

    def observed_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if 'r' in mode:
            resolved = path.resolve()
            assert not any(resolved == item or resolved.is_relative_to(item) for item in forbidden), resolved
            reads.append(resolved)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed_open)
    ledger = load_capability_ledger(matrix, pack_roots=(pack,))
    assert ledger['sources']['rendering_backends'] == [{
        'id': 'render_fixture.chosen', 'required_binaries': ['selected-renderer'], 'source': str(renderer),
    }]
    assert renderer in reads
    assert load_capability_ledger(matrix, pack_roots=())['sources']['rendering_backends'] == []
    assert not capability_execution_admission(ledger, 'ambient_fixture.generate_image')['executable']
    # Changing bytes outside the selected declaration cannot change the ledger.
    forbidden[2].write_text('id: changed.unselected.payload\n')
    assert load_capability_ledger(matrix, pack_roots=(pack,)) == ledger


def test_unsupported_explicit_selection_refuses_before_payload_read(monkeypatch, tmp_path):
    from astrid.core.execution.capability_ledger import load_capability_ledger, capability_execution_admission

    project = tmp_path / 'project'
    matrix = project / 'config/astrid-beta-capabilities.json'
    matrix.parent.mkdir(parents=True)
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': [{
        'id': 'unsupported_fixture.generate_image', 'disposition': 'optional', 'evidence_reason': 'fixture',
    }]}))
    models = project / 'astrid/core/model_catalog/models.yaml'
    models.parent.mkdir(parents=True)
    models.write_text('models: []\n')
    pack = _write_pack(tmp_path / 'selected', 'unsupported_fixture', schema_version=999)
    payload = pack / 'must-not-read.py'
    payload.write_text('raise AssertionError("unadmitted payload")\n')
    original = Path.open

    def observed_open(path, *args, **kwargs):
        assert path.resolve() != payload
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed_open)
    _write_fixture_census(matrix.parent)
    ledger = load_capability_ledger(matrix, pack_roots=(pack,))
    assert all(section['complete'] for section in ledger['sources']['coverage'].values())
    assert not any(row['id'].startswith('unsupported_fixture.') for row in ledger['sources']['executor_inventory'])
    assert not capability_execution_admission(ledger, 'unsupported_fixture.generate_image')['executable']



def _write_fixture_census(config_root, executor_ids=()):
    (config_root / 'astrid-capability-census.lock.json').write_text(json.dumps({
        'schema_version': 1,
        'source_labels': [],
        'historical_source_labels': [
            'iteration.collect_thread_provenance', 'iteration.prepare_iteration',
            'training.manage_asset_cache', 'typed_timeline.typed_timeline.render',
        ],
        'executor_inventory': ['iteration.prepare', 'training.asset_cache', *executor_ids],
    }))


def test_real_selected_executor_has_positive_ledger_admission(monkeypatch, tmp_path):
    from astrid.core.execution.capability_ledger import load_capability_ledger, capability_execution_admission

    pack = _write_pack(tmp_path / 'selected', PACK_ID)
    data = _manifest(PACK_ID)
    data['actions']['generate_image']['metadata']['output_result_manifest'] = True
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    project = tmp_path / 'project'
    matrix = project / 'config/astrid-beta-capabilities.json'
    matrix.parent.mkdir(parents=True)
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': [{
        'id': ACTION_ID, 'disposition': 'optional', 'evidence_reason': 'selected fixture',
    }]}))
    models = project / 'astrid/core/model_catalog/models.yaml'
    models.parent.mkdir(parents=True)
    models.write_text('models: []\n')
    _write_fixture_census(matrix.parent, [ACTION_ID])
    ambient = _write_pack(tmp_path / 'ambient', 'ambient_fixture')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(ambient))
    ledger = load_capability_ledger(matrix, pack_roots=(pack,))
    selected = [row for row in ledger['sources']['executor_inventory'] if row['id'] == ACTION_ID]
    assert len(selected) == 1 and selected[0]['result_contract'] == 'manifest'
    assert selected[0]['source'] == str(pack / 'pack.yaml')
    assert selected[0]['discovery_status'] == 'discovered'
    decision = capability_execution_admission(ledger, ACTION_ID)
    assert decision['executable'] is True and decision['requires_runtime_readiness'] is True
    assert not capability_execution_admission(ledger, 'ambient_fixture.generate_image')['executable']


def test_real_selected_host_shares_one_ledger_snapshot(monkeypatch, tmp_path):
    from astrid.core.execution.generic_host import GenericPackHost

    pack = _write_pack(tmp_path / 'selected', PACK_ID)
    data = _manifest(PACK_ID)
    data['actions']['generate_image']['metadata']['output_result_manifest'] = True
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    project = tmp_path / 'project'
    matrix = project / 'config/astrid-beta-capabilities.json'
    matrix.parent.mkdir(parents=True)
    row = {'id': ACTION_ID, 'disposition': 'optional', 'evidence_reason': 'selected fixture'}
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': [row]}))
    models = project / 'astrid/core/model_catalog/models.yaml'
    models.parent.mkdir(parents=True)
    models.write_text('models: []\n')
    _write_fixture_census(matrix.parent, [ACTION_ID])
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'empty-home'))
    original = Path.open
    matrix_reads = []

    def observed_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if 'r' in mode and path.resolve() == matrix.resolve():
            matrix_reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed_open)
    host = GenericPackHost(pack_roots=[pack], capability_matrix=matrix, credential_source={})
    selected = {item['id'] for item in host.ledger['sources']['executor_inventory']
                if item['discovery_status'] == 'discovered'}
    assert selected == {ACTION_ID}
    assert host.ledger['sources']['selection_explicit'] is True
    assert host.matrix == {ACTION_ID: row}
    assert len(matrix_reads) == 1


def test_selected_local_parent_keeps_its_own_inventory_and_admission(monkeypatch, tmp_path):
    from copy import deepcopy
    from astrid.core.execution.capability_ledger import load_capability_ledger, capability_execution_admission
    from astrid.core.execution.executor.registry import ExecutorRegistry
    from astrid.core.execution.executor.actions import action_executor_definition
    from astrid.core.pack import load_pack_manifest

    pack = _write_pack(tmp_path / 'selected', PACK_ID)
    data = _manifest(PACK_ID)
    child = data['actions']['generate_image']
    child['metadata']['output_result_manifest'] = True
    parent = deepcopy(child)
    parent['metadata'] = {'local_orchestrator': True, 'output_result_manifest': True}
    parent['graph'] = {'child_actions': [ACTION_ID]}
    data['actions']['parent'] = parent
    parent_id = f'{PACK_ID}.parent'
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    matrix = tmp_path / 'project/config/astrid-beta-capabilities.json'
    matrix.parent.mkdir(parents=True)
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': [
        {'id': identifier, 'disposition': 'optional', 'evidence_reason': 'selected fixture'}
        for identifier in (ACTION_ID, parent_id)
    ]}))
    models = matrix.parent.parent / 'astrid/core/model_catalog/models.yaml'
    models.parent.mkdir(parents=True)
    models.write_text('models: []\n')
    _write_fixture_census(matrix.parent, [ACTION_ID, parent_id])
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'empty-home'))
    ledger = load_capability_ledger(matrix, pack_roots=(pack,))
    installed = {row['id']: row for row in ledger['sources']['executor_inventory']
                 if row['discovery_status'] == 'discovered'}
    assert set(installed) == {ACTION_ID, parent_id}
    assert installed[ACTION_ID]['kind'] == 'executor'
    assert installed[parent_id]['kind'] == 'orchestrator'
    assert installed[parent_id]['source'] == str(pack / 'pack.yaml')
    assert installed[parent_id]['result_contract'] == 'manifest'
    assert capability_execution_admission(ledger, parent_id)['executable'] is True
    assert capability_execution_admission(ledger, parent_id)['requires_runtime_readiness'] is True

    # An explicitly selected executor registry grants only its own definitions;
    # sharing the owner's manifest cannot add an unselected parent to that input.
    definition = load_pack_manifest(pack / 'pack.yaml')
    selected = ExecutorRegistry()
    selected.register(action_executor_definition(definition, 'generate_image', definition.actions['generate_image']))
    executor_only = load_capability_ledger(matrix, executor_registry=selected)
    assert parent_id not in {row['id'] for row in executor_only['sources']['executor_inventory']}
    assert capability_execution_admission(executor_only, parent_id)['executable'] is False


def test_public_unsupported_selection_refuses_before_runtime(monkeypatch, tmp_path, capsys):
    from astrid import console
    from astrid.core.generation import cli

    root = tmp_path / 'unsupported'
    _write_pack(root, 'unsupported_fixture', schema_version=999)
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'empty-home'))

    def no_runtime_acquisition():
        pytest.fail('unsupported selection attempted Runtime acquisition')

    monkeypatch.setattr(cli, '_open_client', no_runtime_acquisition)
    assert console.main(['generate', 'image', 'fixture prompt', '--entry', 'unsupported_fixture.image']) == 2
    message = capsys.readouterr().err
    assert 'unknown generation entry' in message and 'unsupported_fixture.image' in message


def test_selected_model_policy_also_blocks_its_public_catalog_alias(monkeypatch, tmp_path):
    root = tmp_path / 'selected'
    pack = _write_pack(root, 'policy_fixture')
    data = _manifest('policy_fixture')
    data['extensions'] = {'astrid': {'generation_availability': {
        'models': {'z-image': _policy(False, 'selected model qualification is absent')},
    }}}
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'empty-home'))
    rows = listing.catalog_rows()
    canonical = [row for row in rows if row['id'] == 'z-image' and row['source'] == 'catalog']
    aliases = [row for row in rows if row['id'] == 'z-image-turbo' and row['source'] == 'catalog-alias']
    assert canonical and aliases
    for row in canonical + aliases:
        assert row['availability'] == 'unavailable'
        assert row['reason'] == 'selected model qualification is absent'
        assert row['fix'] == POLICY_FIX



def test_selected_ledger_uses_only_the_actual_matrix_snapshot(monkeypatch, tmp_path):
    from astrid.core.execution.capability_ledger import load_capability_ledger, capability_execution_admission

    pack = _write_pack(tmp_path / 'selected', PACK_ID)
    data = _manifest(PACK_ID)
    data['actions']['generate_image']['metadata']['output_result_manifest'] = True
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data), encoding='utf-8')
    project = tmp_path / 'project'
    matrix = project / 'config/selected-capabilities.json'
    matrix.parent.mkdir(parents=True)
    matrix.write_text(json.dumps({'schema_version': 1, 'capabilities': [
        {'id': ACTION_ID, 'disposition': 'optional', 'evidence_reason': 'selected fixture'},
        {'id': 'legacy_fixture.absent', 'disposition': 'optional', 'evidence_reason': 'selected history',
         'historical_result_contract': {'external_contract': 'fixture_history',
            'note': 'selected matrix owns this history', 'reasons': ['external-escape-hatch']}},
    ], 'external_contracts': {'fixture_history': {'note': 'selected optional history'}}}))
    models = project / 'astrid/core/model_catalog/models.yaml'
    models.parent.mkdir(parents=True)
    models.write_text('models: []\n')
    _write_fixture_census(matrix.parent, [ACTION_ID, 'legacy_fixture.absent'])
    unselected = matrix.with_name('astrid-beta-capabilities.json')
    unselected.write_text('unselected matrix is not JSON')
    original = Path.open

    def observed_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if 'r' in mode:
            assert path.resolve() != unselected
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed_open)
    ledger = load_capability_ledger(matrix, pack_roots=(pack,))
    historical = [row for row in ledger['sources']['executor_inventory'] if row['id'] == 'legacy_fixture.absent']
    assert len(historical) == 1
    assert historical[0]['source'] == 'config/selected-capabilities.json'
    assert historical[0]['reason'] == 'selected matrix owns this history'
    assert ledger['sources']['fixture_history']['executor_ids'] == ['legacy_fixture.absent']
    assert capability_execution_admission(ledger, ACTION_ID)['executable'] is True
    assert capability_execution_admission(ledger, 'legacy_fixture.absent')['executable'] is False
    unselected.write_text('changed unselected bytes')
    assert load_capability_ledger(matrix, pack_roots=(pack,)) == ledger


def test_description_uses_one_real_availability_snapshot_per_render(monkeypatch, tmp_path):
    from copy import deepcopy
    from astrid.core.generation import availability, entries
    from astrid.core.model_catalog.registry import ModelRegistry

    root = tmp_path / 'selected'
    pack = _write_pack(root, 'description_snapshot', availability=_policy(False, 'old policy'))
    data = _manifest('description_snapshot', availability=_policy(False, 'old policy'))
    data['actions']['generate_image']['metadata']['generation_entry']['modes'] = ['t2i', 'edit']
    manifest = pack / 'pack.yaml'
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(root))
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'astrid-home'))
    original_declaration = availability.availability_declaration
    reads = []

    def observed_declaration(*, selected_packs=None):
        snapshot = original_declaration(selected_packs=selected_packs)
        reads.append(snapshot)
        if len(reads) == 1:
            changed = deepcopy(data)
            changed['extensions']['astrid']['generation_availability']['actions'][
                'description_snapshot.generate_image'
            ] = _policy(True, 'new policy')
            manifest.write_text(yaml.safe_dump(changed, sort_keys=False), encoding='utf-8')
        return snapshot

    monkeypatch.setattr(availability, 'availability_declaration', observed_declaration)
    # Use the real action discovery and a real empty model registry. The wrapper
    # observes, but does not synthesize or replace, the availability declaration.
    first = entries.describe_entry('description_snapshot.image', registry=ModelRegistry([]), _native=True)
    assert len(reads) == 1
    assert {row['availability'] for row in first['mode_descriptions'].values()} == {'unavailable'}
    assert {row['reason'] for row in first['mode_descriptions'].values()} == {'old policy'}

    second = entries.describe_entry('description_snapshot.image', registry=ModelRegistry([]), _native=True)
    assert len(reads) == 2
    assert {row['availability'] for row in second['mode_descriptions'].values()} == {'available'}


def test_default_parent_inventory_skips_legacy_folder_orchestrator_policy(tmp_path, monkeypatch):
    from copy import deepcopy
    from astrid.core.execution.capability_ledger import _executor_inventory
    from astrid.core.execution.executor.registry import load_default_registry as load_executor_registry
    from astrid.core.execution.orchestrator.registry import load_default_registry as load_orchestrator_registry

    project = tmp_path / 'project'
    pack = _write_pack(project / 'astrid/packs', 'inventory_fixture')
    data = _manifest('inventory_fixture')
    child = data['actions']['generate_image']
    child_id = 'inventory_fixture.generate_image'
    child['metadata']['output_result_manifest'] = True
    parent = deepcopy(child)
    parent['metadata'] = {'local_orchestrator': True, 'output_result_manifest': True}
    parent['graph'] = {'child_actions': [child_id]}
    data['actions']['parent'] = parent
    (pack / 'pack.yaml').write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
    monkeypatch.setenv('ASTRID_HOME', str(tmp_path / 'astrid-home'))
    legacy = pack / 'legacy-compose'
    legacy.mkdir()
    (legacy / 'orchestrator.yaml').write_text(
        "schema_version: 1\nid: inventory_fixture.legacy_compose\nname: Legacy Compose\n"
        "kind: external\nversion: '1.0.0'\n"
        "runtime:\n  kind: command\n  command:\n    argv: ['{python_exec}', '-c', 'pass']\n"
        "inputs: []\noutputs: []\n",
        encoding='utf-8',
    )
    # A project root selects its local pack; other installed packs enter the
    # default inventory through an explicit discovery root.
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(pack))

    executors = load_executor_registry(project_root=project)
    orchestrators = load_orchestrator_registry(executor_registry=executors, project_root=project)
    parent_id = 'inventory_fixture.parent'
    legacy_id = 'inventory_fixture.legacy_compose'
    assert parent_id not in {item.id for item in executors.list()}
    assert {item.id for item in orchestrators.list()} >= {parent_id, legacy_id}
    assert orchestrators.get(legacy_id).metadata.get('pack_root') is None
    assert orchestrators.get(legacy_id).metadata.get('content_root') == str(legacy.resolve())

    projected = _executor_inventory(project)
    relevant = {row['id']: row for row in projected if row['id'] in {child_id, parent_id, legacy_id}}
    assert set(relevant) == {child_id, parent_id}
    assert relevant[child_id]['kind'] == 'executor'
    assert relevant[parent_id]['kind'] == 'orchestrator'
    assert relevant[parent_id]['source'] == str((pack / 'pack.yaml').relative_to(project))
