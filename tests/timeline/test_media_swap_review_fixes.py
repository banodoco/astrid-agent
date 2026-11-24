"""Regression coverage for media-swap capture at publication boundaries."""
import copy

from astrid.packs.rendering.docs.scripts.timeline_document import emit_document_publish_commands
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.media_swap import capture_document_swaps
from tests.timeline.test_occurrence_media_swap import (
    CLIP, NEW_ID, OWNER, CatalogTransport, _candidate, _legacy_bundle,
)


def test_capture_reuses_enriched_local_file_verification():
    base = _legacy_bundle()
    candidate = _candidate(base, 'image')
    transport = CatalogTransport()
    selection = transport.get_project_media_selection(base['project_id'], {'asset': 'new-footage'})
    cached = {**transport.get_project_verified_media(base['project_id'], selection),
              'object_id': NEW_ID, 'pin': {'media_id': NEW_ID, 'content_digest': NEW_ID}}
    commands, working, verified = capture_document_swaps(base, candidate, transport, [cached])
    assert len([row for row in verified if row.get('pin') == cached['pin']]) == 1
    result = execute(base, commands, verified_media=verified).bundle
    assert State(result).entity(CLIP) == State(working).entity(CLIP)


def test_publisher_assigns_media_to_previously_unbound_clip():
    base = _legacy_bundle()
    base['base_parent']['publication_id'] = 'publication-1'
    State(base).entity(CLIP).pop('asset')
    candidate = copy.deepcopy(base)
    State(candidate).entity(CLIP)['asset'] = 'picture'
    verified = []
    commands = emit_document_publish_commands(base, candidate, {}, verified, transport=CatalogTransport())
    result = execute(base, commands, verified_media=verified).bundle
    assert State(result).entity(CLIP)['asset'] == 'picture'
    assert State(result).registry(OWNER) == State(candidate).registry(OWNER)


def test_multiple_swaps_have_stable_commands_across_hash_seeds():
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = '''
import copy, json
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.media_swap import capture_document_swaps
from tests.timeline.test_occurrence_media_swap import _legacy_bundle, _candidate, OWNER
base = _legacy_bundle()
clips = State(base).collection(OWNER, 'clip')
for name in ('zeta', 'alpha', 'omega', 'beta', 'gamma', 'delta'):
    row = copy.deepcopy(clips[0]); row['id'] = name; clips.append(row)
candidate = _candidate(base, 'image')
assets = State(candidate).registry(OWNER)['assets']
for row in State(candidate).collection(OWNER, 'clip'):
    alias = 'new-' + row['id']
    assets[alias] = copy.deepcopy(assets['new-footage'])
    row['asset'] = alias
from tests.timeline.test_occurrence_media_swap import CatalogTransport
commands, _, _ = capture_document_swaps(base, candidate, CatalogTransport())
print(json.dumps(commands, sort_keys=True))
'''
    results = [subprocess.check_output([sys.executable, '-c', script], text=True,
               cwd=Path(__file__).resolve().parents[2],
               env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
                    'PYTHONHASHSEED': str(seed)}) for seed in (1, 2, 3)]
    assert all(json.loads(result) == json.loads(results[0]) for result in results[1:])
