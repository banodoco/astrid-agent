"""The project compiler captures only the selected rendering contract."""
from pathlib import Path

import pytest

from astrid.core.element import project_packs
from astrid.core.rendering import _effect_registry
from tests.core.test_project_packs import configured, seed


def _selected_contract(tmp_path, monkeypatch):
    root = tmp_path / 'rendering'
    contract = root / 'shared' / 'element_contracts.ts'
    contract.parent.mkdir(parents=True)
    contract.write_text("export const narrowParams = () => 'selected-contract-marker';\n")
    monkeypatch.setattr(_effect_registry, 'render_pack_roots', lambda: [
        {'root': str(root), 'scope': 'pack-rendering-elements'}])
    return contract


def test_project_admission_compiles_and_freezes_selected_rendering_contract(configured, tmp_path, monkeypatch):
    contract = _selected_contract(tmp_path, monkeypatch)
    _, element, _ = seed()
    (element / 'component.tsx').write_text(
        "import {narrowParams} from '@astrid-rendering/shared/element_contracts';\n"
        "export default function Reveal(){ return narrowParams(); }\n")
    record = project_packs.admit_project_pack('realm', 'project')['elements'][0]
    compiled = Path(record['snapshot']['root']) / record['compiled']['path']
    original = compiled.read_bytes()
    assert b'selected-contract-marker' in original
    assert b'@astrid-rendering' not in original
    contract.write_text("export const narrowParams = () => 'changed';\n")
    assert compiled.read_bytes() == original


def test_project_admission_rejects_other_rendering_imports(configured):
    _, element, _ = seed()
    (element / 'component.tsx').write_text(
        "import x from '@astrid-rendering/shared/other'; export default x;\n")
    with pytest.raises(ValueError, match='Unsupported dependency'):
        project_packs.admit_project_pack('realm', 'project')


def test_project_admission_rejects_contract_symlink_escape(configured, tmp_path, monkeypatch):
    contract = _selected_contract(tmp_path, monkeypatch)
    outside = tmp_path / 'outside.ts'
    outside.write_text("export const narrowParams = () => 1;\n")
    contract.unlink()
    contract.symlink_to(outside)
    _, element, _ = seed()
    (element / 'component.tsx').write_text(
        "import {narrowParams} from '@astrid-rendering/shared/element_contracts'; export default narrowParams;\n")
    with pytest.raises(ValueError, match='escapes its pack'):
        project_packs.admit_project_pack('realm', 'project')


def test_project_admission_uses_discovered_rendering_contract(configured):
    _, element, _ = seed()
    (element / 'component.tsx').write_text(
        "import {narrowParams} from '@astrid-rendering/shared/element_contracts';\n"
        "export default function Reveal(){ return narrowParams({ok: true}); }\n")
    record = project_packs.admit_project_pack('realm', 'project')['elements'][0]
    compiled = (Path(record['snapshot']['root']) / record['compiled']['path']).read_text()
    assert 'narrowParams' in compiled
    assert '@astrid-rendering' not in compiled


def test_project_admission_refuses_external_runtime_dependency_in_contract(configured, tmp_path, monkeypatch):
    contract = _selected_contract(tmp_path, monkeypatch)
    (tmp_path / 'outside.ts').write_text("export default 'outside';\n")
    contract.write_text("import outside from '../../outside'; export const narrowParams = () => outside;\n")
    _, element, _ = seed()
    (element / 'component.tsx').write_text(
        "import {narrowParams} from '@astrid-rendering/shared/element_contracts'; export default narrowParams;\n")
    with pytest.raises(ValueError, match='unsupported runtime dependency'):
        project_packs.admit_project_pack('realm', 'project')
