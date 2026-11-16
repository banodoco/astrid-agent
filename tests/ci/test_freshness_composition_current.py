"""The current placement artifact has an independent freshness gate."""
import json
import shutil
from pathlib import Path
from unittest.mock import patch

from scripts.ci import freshness as f


def test_current_composition_checks_digest_source_version_and_path(tmp_path):
    source = Path(__file__).resolve().parents[2]
    archive = source / 'remotion/artifacts/banodoco-timeline-composition-0.0.19.tgz'
    roots = [tmp_path / 'agent/remotion', tmp_path / 'app']
    for root, directory in zip(roots, ['artifacts', 'vendor/artifacts']):
        (root / directory).mkdir(parents=True)
        shutil.copyfile(archive, root / directory / archive.name)
        path = f'file:{directory}/{archive.name}'
        (root / 'package.json').write_text(json.dumps({'dependencies': {'@banodoco/timeline-composition': path}}))
        (root / 'package-lock.json').write_text(json.dumps({'packages': {'node_modules/@banodoco/timeline-composition': {
            'resolved': path, 'version': f.CURRENT_VERSION, 'integrity': f.CURRENT_INTEGRITY}}}))
    args = (tmp_path / 'agent', tmp_path / 'app', archive)
    assert f.evaluate_current_composition(*args)['result'] == 'pass'
    for key, bad in [('CURRENT_COMPOSITION', 'bad'), ('CURRENT_SOURCE', 'bad'), ('CURRENT_VERSION', 'bad')]:
        with patch.object(f, key, bad):
            assert f.evaluate_current_composition(*args)['result'] == 'fail'
    package = roots[1] / 'package.json'
    package.write_text(json.dumps({'dependencies': {'@banodoco/timeline-composition': 'file:wrong'}}))
    assert 'path differs' in str(f.evaluate_current_composition(*args)['errors'])
