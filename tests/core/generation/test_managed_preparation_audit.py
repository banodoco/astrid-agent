"""CPU process budgets for one managed job's installation audit."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_runner, entries, managed_preparation
from astrid.core.generation.engine_runner_target import _ENV_PROBE
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from tests.core.generation.test_engine_runner import install  # noqa: F401 (fixture)
from tests.core.generation.test_engine_runner import setup as engine_setup  # noqa: F401 (fixture)
from tests.core.generation.test_run_local import mod


@pytest.fixture
def managed_job(engine_setup, tmp_path, monkeypatch):  # noqa: F811 (fixture)
    plan, context, adapter, deadline = engine_setup
    receipt = install(engine_setup)
    context.events.clear()
    adapter.local_install_lock = lambda root, *, deadline, **kw: context.acquire_install_lock(root, deadline=deadline)
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.root, prep.recipe = plan.root, context.recipe
    prep.context, prep.adapter, prep.deadline = context, adapter, deadline
    prep.task, prep.attempt, prep.mode = 'audit-task', 'attempt', 'align'
    prep.device = prep.visibility = prep.logs_root = prep.lock = None
    prep.cancelled = lambda: False
    prep._preparing_engine = prep.uncertain = False
    prep.scratch = tmp_path / 'scratch'
    prep._guard_runtime = lambda *a, **kw: None
    prep._runtime_launch_flags, prep._sync_packages = [], []
    canonical = prep.scratch / 'canonical/workflow.py'
    canonical.parent.mkdir(parents=True)
    canonical.write_text('# canonical CPU fixture\n')
    manifest = {'engine': 'comfyui', 'base': None, 'models': [],
                'extra': {'nodes': [{'repo': 'https://example.test/node', 'commit': 'a' * 40}]},
                'models_root': str(tmp_path / 'models')}
    monkeypatch.setattr(managed_preparation, 'judge_declarations', lambda *a: (manifest, 'scratchpad'))
    monkeypatch.setattr(managed_preparation, 'configured_models_root', lambda **kw: manifest['models_root'])
    monkeypatch.setattr(entries, 'engine_table', lambda: {'comfyui': mod.ComfyUIEngine()})
    state = SimpleNamespace(pip_existed=True, nodes_changed=False, synced=False,
                            fetch_output='skipped model (cached sha256)\n', native=[])

    def native(_program, *args, step):
        assert context.held, 'native alignment must retain install custody'
        state.native.append(step)
        facts = {'pip_bootstrap': {'existed': state.pip_existed},
                 'nodes_restore': {'changed': state.nodes_changed},
                 'prepare': {'synchronization': {'synced': state.synced}}}
        return context.result(state.fetch_output if step == 'fetch' else json.dumps(facts.get(step, {})))

    prep._native = native
    source = tmp_path / 'workflow.json'
    source.write_text('{}')

    def run():
        return prep.prepare(source, execution_identity='execution', inputs={},
                            canonical_workflow=None, input_digests={})

    yield SimpleNamespace(prep=prep, run=run, context=context, state=state, receipt=receipt)
    prep._preparing_engine = False  # FakeContext has no live process to stop.
    prep.close()


def audit_counts(job):
    argv = [event[1] for event in job.context.events if event[0] == 'argv']
    return {
        'git': sum(command[0] == 'git' for command in argv),
        'environment': sum('engine_runner_target' in ' '.join(command)
                           and command[-2] == 'environment-spec' for command in argv),
        'probe': sum(len(command) > 3 and command[-2] == _ENV_PROBE for command in argv),
        'pip_check': sum(command[0].endswith('/uv') and 'check' in command for command in argv),
        'patches': sum('engine_runner_target' in ' '.join(command)
                       and command[-2] == 'verify-source-patches' for command in argv),
    }


def assert_audits(job, count):
    recipe = job.prep.recipe
    assert audit_counts(job) == {
        'git': count * 3 * len(recipe.sources),
        'environment': count * len(recipe.environments),
        'probe': count * len(recipe.environments),
        'pip_check': count * len(recipe.environments),
        'patches': count * len({patch.source for patch in recipe.source_patches}),
    }


@pytest.mark.parametrize('mode', ['align', 'check', 'trust'])
def test_unchanged_job_runs_one_installation_audit(managed_job, mode):
    job = managed_job
    job.prep.mode = mode
    job.run()
    assert_audits(job, 1)
    if mode == 'align':
        assert job.state.native == ['pip_bootstrap', 'nodes_restore', 'prepare', 'doctor']


@pytest.mark.parametrize('change', ['nodes', 'sync', 'pip'])
def test_real_alignment_change_keeps_later_audits(managed_job, change):
    job = managed_job
    if change == 'nodes':
        job.state.nodes_changed = True
    elif change == 'sync':
        job.state.synced = True
    else:
        job.state.pip_existed = False  # Cold node setup installs bundled pip.
    job.run()
    assert_audits(job, 3)


def test_releasing_and_reacquiring_install_lock_requires_fresh_audit(managed_job):
    job = managed_job
    job.run()
    job.prep._preparing_engine = False
    job.prep.close()
    job.run()
    assert_audits(job, 2)


def test_same_runner_cannot_reuse_audit_across_lock_acquisitions(managed_job):
    job = managed_job
    job.run()
    job.prep.lock.__exit__(None, None, None)
    assert not job.context.held
    job.prep.lock = job.prep._installation_lock(None)
    job.prep.lock.__enter__()
    job.prep._verify_alignment(*job.prep._alignment_verification)
    assert_audits(job, 2)


@pytest.mark.parametrize('output, count', [('skipped model (cached sha256)\n', 1),
                                         ('downloaded model -> /models/model\n', 2)])
def test_fetch_outcome_controls_audit_reuse(managed_job, output, count):
    job = managed_job
    job.run()
    job.state.fetch_output = output
    job.prep._command(['fetch', str(job.prep.scratch / 'canonical/workflow.py')])
    job.prep._verify_alignment(*job.prep._alignment_verification)
    assert_audits(job, count)


def test_changed_source_is_refused_after_alignment_mutation(managed_job):
    job = managed_job
    job.state.synced = True
    native = job.prep._native

    def mutate(program, *args, step):
        result = native(program, *args, step=step)
        if step == 'prepare':
            next(iter(job.context.sources.values()))['head'] = '0' * 40
        return result

    job.prep._native = mutate
    with pytest.raises(engine_runner.IntegrityError, match='source revision differs'):
        job.run()


@pytest.mark.parametrize('commands, changed', [
    ([['git', '-C', '/node', 'rev-parse', 'HEAD'],
      ['git', '-C', '/node', 'status', '--porcelain']], False),
    ([['git', '-C', '/node', 'fetch', 'origin'],
      ['git', '-C', '/node', 'checkout', 'a' * 40]], True),
    ([['python', '-m', 'pip', 'install', '--dry-run', '--report', '/plan', 'node-dep']], False),
    ([['python', '-m', 'pip', 'install', 'node-dep']], True),
])
def test_native_node_restore_reports_executed_changes(monkeypatch, commands, changed):
    import subprocess

    from vibecomfy import cli
    from vibecomfy.commands import nodes

    from astrid.packs.vibecomfy.native_preparation import restore_native_nodes

    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw:
                        calls.append(command) or SimpleNamespace(returncode=0))

    def restore(entry, *, runner):
        for command in commands:
            runner(command, check=True)
        return SimpleNamespace(status='refreshed')

    monkeypatch.setattr(nodes.node_packs_install, 'restore_pack', restore)
    monkeypatch.setattr(cli, 'main', lambda argv:
                        0 if nodes.node_packs_install.restore_pack('entry').status == 'refreshed' else 1)
    assert restore_native_nodes(['nodes', 'restore']) == {'changed': changed}
    assert calls == commands
    assert nodes.node_packs_install.restore_pack is restore


def test_native_matching_node_restore_uses_real_readonly_outcome(tmp_path, monkeypatch):
    import subprocess

    from astrid.packs.vibecomfy.native_preparation import restore_native_nodes

    root = tmp_path / 'nodes'
    (root / 'node').mkdir(parents=True)
    monkeypatch.setenv('VIBECOMFY_CUSTOM_NODES_DIR', str(root))
    lock = tmp_path / 'nodes.lock'
    lock.write_text('[nodepacks.node]\nurl = "https://example.test/node"\ncommit = "' + 'a' * 40 + '"\n')
    calls = []

    def run(command, **kw):
        assert command[:3] == ['git', '-C', str(root / 'node')]
        assert command[3:] in [['rev-parse', 'HEAD'], ['status', '--porcelain']]
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='a' * 40 if command[3] == 'rev-parse' else '')

    monkeypatch.setattr(subprocess, 'run', run)
    assert restore_native_nodes(['nodes', '--non-interactive', 'restore', '--lockfile', str(lock)]) == {'changed': False}
    assert len(calls) == 2
