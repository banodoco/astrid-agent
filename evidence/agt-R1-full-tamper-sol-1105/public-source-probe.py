import copy

import pytest
import runtime_protocol.platform

from astrid.sdk import timeline
from astrid.sdk.timeline._common import TimelineEditError
from tests.sdk.test_timeline_public_command import public_session, rename
from tests.sdk.test_timeline_commit_provider import runtime_abi


@pytest.mark.parametrize('damage', ['pair', 'foreign', 'digest'])
def test_source_guard_applies_at_public_open_not_after_cached_read(public_session, tmp_path, damage):
    opened, realm, sent, client, _ = public_session
    verified = copy.deepcopy(opened._runtime_document)
    if damage == 'pair':
        realm.entries[0]['revision_id'] = 'another-revision'
    elif damage == 'foreign':
        realm.documents[realm.head['revision_id']]['project_id'] = 'foreign'
        realm.entries[0]['receipt']['result']['canonical_document'] = realm.documents[realm.head['revision_id']]
    else:
        realm.documents[realm.head['revision_id']]['parent_revision']['content_digest'] = 'sha256:' + '0' * 64
    backing = copy.deepcopy((realm.documents, realm.entries))
    # This draft uses the immutable copy read and verified before backing-store tampering.
    opened.command(rename())
    assert opened._runtime_document == verified
    assert sent == [] and len(opened._commands) == 1
    # A fresh production open actually consumes the damaged source and refuses it.
    with pytest.raises(TimelineEditError):
        timeline.open('project-1', 'film', client=client, outbox_path=tmp_path / 'fresh.json')
    assert (realm.documents, realm.entries) == backing
    assert sent == []
