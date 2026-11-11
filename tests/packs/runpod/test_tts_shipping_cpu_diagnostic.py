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
    from copy import deepcopy
    from functools import lru_cache

    from astrid.core.generation import cli, entries
    from astrid.core.generation.job_inputs import write_archive
    from astrid.packs.runpod import machine as rm
    from astrid.packs.vibecomfy import local_job
    from vibecomfy.workflow_bundle import emit_bundle

    entry, mode = 'audio/qwen3_tts_custom_voice', 'tts'
    # Freeze the pinned repository's discovery for this one test. Each real
    # resolve still validates its own inputs; repeated ready-template scans and
    # authoring-schema construction add work to this bounded CPU diagnostic.
    # Copies prevent either route from mutating the other route's snapshot.
    for name in ('_ready_entries_for', 'describe_entry'):
        cached = lru_cache(maxsize=None)(getattr(entries, name))
        def snapshot(*args, _cached=cached, **kwargs):
            return deepcopy(_cached(*args, **kwargs))
        monkeypatch.setattr(entries, name, snapshot)
    # Synthetic content only: never substitute a real prompt or live job artifacts.
    # Resolve the pinned template's true native contract and entry once. Later
    # direct and shipped calls still run resolve_entry_inputs/validate_inputs;
    # only repeated discovery of this same immutable entry is replaced.
    from copy import deepcopy
    native_description = entries.describe_entry(entry, mode=mode, _native=True)
    described = entries.find_entry(entry)
    assert described is not None
    original_describe, original_find = entries.describe_entry, entries.find_entry
    discovery_hits = {'description': 0, 'entry': 0}

    def cached_describe(entry_id, mode=None, *, registry=None, _native=False):
        if entry_id != entry or mode != 'tts' or registry is not None:
            return original_describe(entry_id, mode=mode, registry=registry, _native=_native)
        discovery_hits['description'] += 1
        result = deepcopy(native_description)
        return result if _native else entries.public_description(result)

    def cached_find(entry_id):
        if entry_id != entry:
            return original_find(entry_id)
        discovery_hits['entry'] += 1
        return deepcopy(described)

    monkeypatch.setattr(entries, 'describe_entry', cached_describe)
    monkeypatch.setattr(entries, 'find_entry', cached_find)
    description, values = entries.resolve_entry_inputs(entry, {'text': 'synthetic fixture'}, mode=mode)
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
    # The shared fake install context creates its deadline during fixture setup;
    # entry discovery here can consume that unrelated wall-clock budget. Begin
    # the fake install window at claim, as the real route does for preparation.
    import time
    machine_case.context.deadline = time.monotonic() + 60
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
    assert discovery_hits['description'] >= 3 and discovery_hits['entry'] >= 2

    # Exercise the later engine preflight separately with a fake target: the
    # fixture is a projection of the pinned Vibe cache, not a live observation.
    # Reload each sealed bundle independently so target schema metadata captured
    # for one pass cannot mask a mismatch in the other.
    from types import SimpleNamespace
    import pytest
    from astrid.packs.vibecomfy import local_server_engine, production_engine

    fixture = Path(__file__).parents[2] / 'fixtures/tts_cpu/pinned_object_info.json'
    cached = json.loads(fixture.read_text())
    info = {}
    for kind, row in cached.items():
        info[kind] = {'input': row['inputs'],
                      'output': [item['type'] for item in row['outputs']],
                      'output_node': kind == 'SaveAudioMP3'}
    assert set(info) == {'AILab_Qwen3TTSCustomVoice', 'SaveAudioMP3'}
    observed = {'schema': info}
    client = SimpleNamespace(object_info=lambda: deepcopy(observed['schema']))

    def preflight(path, target_schema):
        observed['schema'] = target_schema
        loaded = production_engine.load_workflow_path(path, tmp_path / 'inspect')
        bundle = production_engine._canonical_bundle_value(loaded.resolved)
        return local_server_engine._live_preflight(
            bundle, plan['bindings'], 'http://127.0.0.1:1', tmp_path, client=client,
            target_custody=())

    preflight(root / 'workflow.py', info)
    preflight(landing / 'workflow.py', info)
    changed = deepcopy(info)
    # Remove the retained voice from the target's allowed enum without exposing
    # inputs in logs; this is synthetic drift, not a claim about the real pod.
    choices = changed['AILab_Qwen3TTSCustomVoice']['input']['required']['speaker'][0]
    choices[:] = [choice for choice in choices if choice != 'Ryan']
    with pytest.raises(local_server_engine.ProductionEngineError):
        preflight(root / 'workflow.py', changed)
    with pytest.raises(local_server_engine.ProductionEngineError):
        preflight(landing / 'workflow.py', changed)


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
