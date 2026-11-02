import os
from pathlib import Path
import pytest
from scripts import ci_mac_bundle_b as bundle


def test_branch_mismatch_never_updates_selected_ref(tmp_path, monkeypatch):
    repo = tmp_path / 'agent.git'
    repo.mkdir(mode=0o700)
    monkeypatch.setattr(bundle, 'UID', os.getuid())
    monkeypatch.setattr(bundle, 'verify_cache_tree', lambda _: None)
    monkeypatch.setattr(bundle, 'validate_cache_config', lambda *args: None)
    calls = []
    def git(args, env):
        calls.append(args)
        if '--is-bare-repository' in args:
            return 'true'
        if 'FETCH_HEAD^{commit}' in args:
            return 'a' * 40
        return ''
    monkeypatch.setattr(bundle, 'git', git)
    with pytest.raises(RuntimeError, match='expected commit'):
        bundle.cached_repo(tmp_path, 'agent', 'refs/heads/branch', {}, 'b' * 40)
    assert not any('update-ref' in args or 'symbolic-ref' in args for args in calls)


def test_workflow_contains_exact_reviewed_script():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    script = (root / 'scripts/ci_mac_bundle_b.py').read_text()
    body = workflow.split("<<'PYCODE'\n", 1)[1].rsplit('          PYCODE', 1)[0]
    assert '\n'.join(line[10:] for line in body.splitlines()) + '\n' == script
    assert 'actions/checkout@' not in workflow
    assert 'workflow_dispatch:' in workflow


def test_frozen_bundle_b_source_revisions():
    assert bundle.AGENT_BRANCH_B == "lane/M-R1FIX/UPD-R0-STOPPED-JOB"
    assert bundle.AGENT_SHA_B == "efcb3297ab16a3ec47b30b02154089b372293544"
    assert bundle.PINOKIO_SHA_B == "292ab95d0d95065e449d08d5c170188b72aff520"
