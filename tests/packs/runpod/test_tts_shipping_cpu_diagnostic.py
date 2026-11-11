"""WIP: bounded TTS shipping/rehydration diagnosis; all input is synthetic.

Run only under the host lease, with the pinned VibeComfy checkout installed.
The fake machine transports bytes in memory. The final engine call is a spy:
this does not prove Comfy execution, prepared Runtime health, or provider behavior.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from tests.packs.runpod.test_machine import (  # noqa: F401
    machine_case as machine_case, retained as retained, sources as sources, case as case,
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)


def test_tts_shipped_job_rehydrates_same_public_contract_as_direct(machine_case, tmp_path, monkeypatch, capsys):
    from astrid.core.generation import cli, entries
    from astrid.core.generation.job_inputs import write_archive
    from astrid.packs.runpod import machine as rm
    from astrid.packs.vibecomfy import local_job
    from vibecomfy.workflow_bundle import emit_bundle

    entry, mode = 'audio/qwen3_tts_custom_voice', 'tts'
    # Synthetic content only: never substitute a real prompt or live job artifacts.
    description, values = entries.resolve_entry_inputs(entry, {'text': 'synthetic fixture'}, mode=mode)
    described = entries.find_entry(entry)
    assert described is not None
    assert not entries.entry_hooks(described).get('build'), 'diagnostic must be extended for build hooks'
    workflow = entries._load_entry_workflow(described)
    workflow, plan, assets = entries.apply_workflow_inputs(
        workflow, mode=mode, values=values, binds=described.get('binds'), defaults=described.get('defaults'))
    assert not plan['unbound'] and not assets
    root = tmp_path / 'synthetic'
    root.mkdir()
    emit_bundle(workflow, root / 'workflow.py', {'operation': 'authored'})
    source = root / 'source.json'
    source.write_text(json.dumps({'nodes': [], 'links': []}))
    archive = write_archive(root / 'entry-inputs.zip', values, description['inputs'])
    original = machine_case.job()
    job = replace(original, entry=entry, mode=mode, inputs={
        'python': str(root / 'workflow.py'), 'companion': str(root / 'workflow.vibe.json'),
        'source': str(source), 'entry_inputs': str(archive),
        'workflow_inputs': json.dumps(plan['bindings'], sort_keys=True),
    })
    lease = machine_case.machine.claim(job)
    run = machine_case.machine.start(lease, job)
    raw = machine_case.pod.files[f'{run.remote_root}/{rm.JOB_NAME}']
    payload = json.loads(raw)
    assert payload['schema'] == 'astrid.machine-job.v1'
    assert payload['entry'] == entry and payload['mode'] == mode
    assert payload['inputs'] == values and payload['bindings'] == plan['bindings']
    assert set(payload['execution']) == {'python', 'companion', 'source'}

    # Rehydrate the exact bytes that the machine serialized and uploaded, translating
    # only fake pod paths into this test's temporary directory.
    landing = tmp_path / 'rehydrated'
    landing.mkdir()
    for name, remote in payload['execution'].items():
        local = landing / {'python': 'workflow.py', 'companion': 'workflow.vibe.json',
                           'source': 'source.json'}[name]
        local.write_bytes(machine_case.pod.files[remote])
        payload['execution'][name] = str(local)
    job_file = landing / rm.JOB_NAME
    job_file.write_text(json.dumps(payload))

    calls, schemas = [], []
    monkeypatch.setattr('astrid.core.generation.preflight.require_generation_target_readiness', lambda *a: None)
    monkeypatch.setattr(entries, 'target_schema', lambda *a, **kw: schemas.append(kw.get('align')))
    monkeypatch.setattr(local_job, 'run_owned_action', lambda workflow, out, **kw: calls.append(kw) or {'outputs': []})
    local_job.run_named_entry(described, entry, dict(values), {}, tmp_path / 'direct',
                              task=job.task_id, attempt=job.attempt_id, mode=mode, align=job.align)
    direct = calls.pop()
    argv = rm.generation_argv(entry, str(landing), mode=mode)
    assert argv[1:5] == ['generate', entry, '--placement', 'local']
    assert argv[argv.index('--job') + 1] == str(job_file)
    assert cli._main(argv[2:]) == 0
    capsys.readouterr()  # Never print synthetic or untrusted command output.
    shipped = calls.pop()
    assert shipped['entry'] == direct['entry'] == entry
    assert shipped['align'] == direct['align'] == job.align
    assert json.loads(shipped['workflow_inputs']) == json.loads(direct['workflow_inputs'])
    assert schemas == [job.align]  # prepared non-hook path intentionally does not repeat target_schema


def test_error_frame_keeps_only_allowlisted_structural_facts():
    from astrid.packs.runpod.failure_diagnostics import MARK, parse_frame, summarize
    issue = {'issues': [{'severity': 'error', 'code': 'input_rejected',
                         'message': 'PRIVATE_SENTINEL',
                         'details': {'field': 'text', 'constraint': 'required',
                                     'untrusted': 'PRIVATE_SENTINEL'}}]}
    safe = summarize(issue)
    assert safe == {'code': 'input_rejected', 'category': 'input', 'field': 'text', 'constraint': 'required'}
    assert parse_frame(MARK + json.dumps(safe)) == safe
    assert 'PRIVATE_SENTINEL' not in json.dumps(safe)
