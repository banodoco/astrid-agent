"""Native fork proofs retain exact physical owners and base/landing boundaries."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import commit
from astrid.sdk.timeline._command_state import present
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.editing_conformance.test_provider_occurrence_fork import fork
from tests.editing_conformance.test_recipe_occurrence_fork import fork_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.mark.parametrize('damage', [
    'owners', 'owner-order', 'digest', 'requested-base', 'landing', 'missing',
    'mixed-native', 'stripped-physical', 'policy', 'dependencies',
])
def test_production_fork_reservation_refuses_incomplete_or_mixed_proof(monkeypatch, damage):
    realm = DetachedRealm(fork_document(distinct=True))
    call, base = fork(realm), copy.deepcopy(realm.head)
    submit(realm, patch(change(['name'], present('Renamed Film'), 'Other edit')), reads=[])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.prepared))
    check = commit._check_reservations
    observed = []

    def corrupt(port, plan, current, rejected, requested):
        record = plan.records[0]
        bindings = record['bindings']
        source = next(item for item in bindings if item['key'] == 'fork.source')
        owner = next(item for item in bindings if item['key'] == 'fork.owner')
        assert source['owners'] == ['occurrence:occ-0/shot', 'occurrence:occ-1/shot']
        assert source['boundary']['evaluated_head'] == current
        assert source['boundary']['base_revision_id'] == base['revision_id']
        if damage == 'owners':
            source['owners'].pop()
        elif damage == 'owner-order':
            source['owners'].reverse()
        elif damage == 'digest':
            source['content_digest'] = 'sha256:' + '0' * 64
        elif damage == 'requested-base':
            source['boundary']['base_revision_id'] = current['revision_id']
        elif damage == 'landing':
            source['boundary']['evaluated_head'] = base
        elif damage == 'missing':
            bindings.remove(source)
        elif damage in ('mixed-native', 'stripped-physical'):
            for binding in ([source] if damage == 'mixed-native' else [source, owner]):
                native = {key: binding[key] for key in ('key', 'ref', 'policy', 'dependencies')}
                binding.clear()
                binding.update(native)
        elif damage == 'policy':
            source['policy'] = 'caller'
        else:
            source['dependencies'] = ['unproven']
        observed.append(damage)
        return check(port, plan, current, rejected, requested)

    monkeypatch.setattr(commit, '_check_reservations', corrupt)
    with pytest.raises(CommitRejected):
        submit(realm, call, base=base, reads=[], merge='fields')
    assert observed == [damage]
    assert (realm.head, realm.entries, realm.documents, realm.prepared) == before
