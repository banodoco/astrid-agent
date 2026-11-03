"""Moved policy is validated by real manifest admission, before projection."""
from __future__ import annotations

import pytest
import yaml

from astrid.core.pack import PackValidationError, load_pack_manifest
from tests.core.generation.test_moved_data_composition import _manifest


@pytest.mark.parametrize('policy, message', [
    ({'generation_availability': []}, 'generation_availability must be an object'),
    ({'generation_availability': {'actions': []}}, 'actions must be an object'),
    ({'generation_availability': {'actions': {'policy_fixture.generate_image': {'available': 1}}}}, 'available must be boolean'),
    ({'generation_availability': {'actions': {'policy_fixture.generate_image': {'available': False}}}}, 'error must be an object'),
    ({'generation_availability': {'actions': {'other.generate_image': {'available': True}}}}, 'not owned by pack policy_fixture'),
    ({'generation_availability': {'template_models': {'fixture/': 'absent'}}}, "must reference this pack's model policy"),
    ({'generation_availability': {'fixture_only_routes': {'other:preview.json': True}}}, 'owned fixture route'),
    ({'output_result_exemptions': {'actions': {'absent': {'note': 'not local', 'reasons': ['no-artifact']}}}}, 'unknown local action'),
    ({'output_result_exemptions': {'actions': {'generate_image': {'note': 'invalid reason', 'reasons': ['anything']}}}}, 'reasons is invalid'),
])
def test_real_manifest_refuses_malformed_or_foreign_policy(tmp_path, policy, message):
    pack = tmp_path / 'policy_fixture'
    pack.mkdir()
    data = _manifest('policy_fixture')
    data['extensions'] = {'astrid': policy}
    manifest = pack / 'pack.yaml'
    manifest.write_text(yaml.safe_dump(data), encoding='utf-8')
    with pytest.raises(PackValidationError, match=message):
        load_pack_manifest(manifest)
