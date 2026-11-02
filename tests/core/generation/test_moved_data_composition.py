"""Real installed-pack composition coverage for the public generation listing."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

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
        "pack_api": "1.0",
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


def test_real_selected_manifest_composes_through_generation_cli(monkeypatch, tmp_path, capsys):
    selected_root = tmp_path / "selected-packs"
    unselected_root = tmp_path / "unselected-packs"
    pack_root = _write_pack(selected_root, PACK_ID, availability=_policy(False))
    _write_pack(unselected_root, "unselected_fixture", availability=_policy(False))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(selected_root))
    monkeypatch.setenv("ASTRID_HOME", str(tmp_path / "astrid-home"))

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

    # The installed action is available to real discovery but excluded from the bundled index.
    first_block = gen_capability_index.render_block()
    second_block = gen_capability_index.render_block()
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
        'id': 'policy_fixture.generate_image', 'result_contract': 'exempted',
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
