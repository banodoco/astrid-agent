"""Read-only, bounded observation of #1002 on the unchanged four existing cases."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys

import pytest

CURRENT = None
CAPTURES = {}
LOOPS = {}
AUTH = Path('/workspace/scratch/ed89673be2cd/runtime-sync/runtime_protocol/timeline_commit_auth.py')
WRITE_LOOP = next(i for i, line in enumerate(AUTH.read_text().splitlines(), 1)
                  if line.strip() == 'for write_index, write in enumerate(writes):')


def emit(value):
    with Path(os.environ['S01_OBSERVER_OUTPUT']).open('a') as output:
        output.write(json.dumps(value, sort_keys=True, allow_nan=False) + '\n')


def exact(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def differences(stored, reconstructed, path=()):
    if exact(stored, reconstructed):
        return []
    if isinstance(stored, dict) and isinstance(reconstructed, dict):
        rows = []
        for key in sorted(stored.keys() | reconstructed.keys()):
            if key not in stored or key not in reconstructed:
                rows.append({'path': [*path, key],
                             'stored': {'present': key in stored, 'value': stored.get(key)},
                             'reconstructed': {'present': key in reconstructed, 'value': reconstructed.get(key)}})
            else:
                rows.extend(differences(stored[key], reconstructed[key], (*path, key)))
        return rows
    if isinstance(stored, list) and isinstance(reconstructed, list):
        rows = []
        for index in range(max(len(stored), len(reconstructed))):
            if index >= len(stored) or index >= len(reconstructed):
                rows.append({'path': [*path, index],
                             'stored': {'present': index < len(stored), 'value': stored[index] if index < len(stored) else None},
                             'reconstructed': {'present': index < len(reconstructed), 'value': reconstructed[index] if index < len(reconstructed) else None}})
            else:
                rows.extend(differences(stored[index], reconstructed[index], (*path, index)))
        return rows
    return [{'path': list(path), 'stored': {'present': True, 'value': stored},
             'reconstructed': {'present': True, 'value': reconstructed}}]


def trace(frame, event, arg):
    if (CURRENT is None or frame.f_code.co_filename != str(AUTH)
            or frame.f_code.co_name != '_inverse_stage_boundaries'):
        return None
    state = frame.f_locals
    if event == 'line' and frame.f_lineno == WRITE_LOOP:
        key = (CURRENT, id(frame), id(state['stage']))
        number = LOOPS.get(key, 0)
        LOOPS[key] = number + 1
        emit({'event': 'native_reconstruction', 'nodeid': CURRENT,
              'snapshot': number, 'after_write_index': None if number == 0 else state.get('write_index'),
              'source_boundary': state.get('source_boundary'),
              'last_write': None if number == 0 else state.get('write'),
              'internal_images': {key: copy.deepcopy(value) for key, value in state['native_image'].items()
                                  if key.startswith('internal-revision:')}})
    if event == 'exception' and 'inverse stage materialization changes internal payload' in str(arg[1]):
        expected, image = state['expected'], state['image']
        delta = differences(expected['payload'], image['payload'])
        captured = {'event': 'inverse_refusal', 'nodeid': CURRENT,
                    'exception_type': arg[0].__name__, 'exception_message': str(arg[1]),
                    'failure': copy.deepcopy(getattr(arg[1], 'failure', None)),
                    'old_ref': state['old'], 'new_ref': state['new'],
                    'stored_expected_image': copy.deepcopy(expected),
                    'reconstructed_image': copy.deepcopy(image),
                    'stage_before_image': copy.deepcopy(state['stage_before'].get(state['old'])),
                    'stage_source': copy.deepcopy(state['source_boundary']),
                    'native_writes': copy.deepcopy(state['writes']),
                    'source_step': copy.deepcopy(state['source_step']),
                    'first_difference': delta[0] if delta else None, 'all_differences': delta}
        CAPTURES[CURRENT] = captured
        emit(captured)
    return trace


def pytest_configure():
    sys.settrace(trace)


def pytest_runtest_setup(item):
    global CURRENT
    CURRENT = item.nodeid


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != 'call' or item.nodeid not in CAPTURES:
        return
    captured = CAPTURES[item.nodeid]
    expected = captured['stored_expected_image']
    editing = item.funcargs['editing']
    try:
        stored = editing.service.get_project_timeline_revision(
            editing.project, expected['timeline_id'], expected['revision_id'])
        emit({'event': 'public_stored_readback', 'nodeid': item.nodeid,
              'call_outcome': report.outcome, 'timeline_id': expected['timeline_id'],
              'revision_id': expected['revision_id'],
              'stored_payload_matches_expected': exact(stored['payload'], expected['payload']),
              'stored_digest_matches_expected': stored['content_digest'] == expected['content_digest'],
              'stored': stored})
    except Exception as exc:
        emit({'event': 'observer_readback_error', 'nodeid': item.nodeid,
              'exception_type': type(exc).__name__, 'exception_message': str(exc)})


def pytest_unconfigure():
    sys.settrace(None)
