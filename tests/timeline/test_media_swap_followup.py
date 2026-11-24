import copy
import json

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commit import _manifest
from astrid.sdk.timeline.media_swap import capture_document_swaps
from tests.timeline.test_occurrence_media_swap import (
    NEW_ID, OWNER, CatalogTransport, _candidate, _legacy_bundle,
)


def test_reused_manifest_object_checks_runtime_kind():
    base = _legacy_bundle()
    document = base['source_mapping']['_runtime_document']
    manifest = document['dependency_manifest']
    pin = {'media_id': NEW_ID, 'content_digest': NEW_ID}
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        manifest['media'] + [pin], manifest['documents'],
    )
    candidate = _candidate(base, 'image')
    entry = State(candidate).registry(OWNER)['assets']['new-footage']
    entry.pop('generationId')
    entry.pop('variantId')
    with pytest.raises(ValueError, match='Runtime metadata'):
        capture_document_swaps(base, candidate, CatalogTransport(kind='audio'))


def test_interrupted_cli_request_retains_source_in_frozen_outbox(tmp_path, monkeypatch):
    from tests.sdk.test_timeline_save_interrupted_l3 import BASE, _failure, _session
    session = _session(tmp_path, monkeypatch)
    error = session.save_interrupted(
        [{'op': 'timeline.move', 'args': {'on': 1}}], base=copy.deepcopy(BASE),
        label='Swap', steps=[], merge='atomic', reads=None, key='key',
        evaluation={'kind': 'function'}, failure=_failure(), via='cli',
    )
    record = session.outbox.read()['pending_changes'][error.pending_id]
    assert json.loads(record['flight']['body'])['source'] == {'via': 'cli'}
