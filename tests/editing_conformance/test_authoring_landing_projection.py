"""Offline landing/prepared parity through the mounted Agent commit caller."""
from __future__ import annotations

import copy
import json
import sys

import pytest
import runtime_protocol
from runtime_protocol.timeline_commit_auth import validate_sequential_capture
from runtime_protocol.timeline_commit_store import detached

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError, present
from astrid.sdk.timeline._commit_patch import CommitPatch
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.editing_conformance.test_occurrence_media_manifest_inverse import accepted_records
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.editing_conformance.test_recipe_occurrence_media import (
    same_alias_realm as same_alias_realm,
)
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.mark.parametrize('dry_run', [False, True])
def test_same_alias_inverse_landing_matches_prepared_document(
    request, monkeypatch, tmp_path, dry_run,
):
    # These are generated offline fixture values, not the missing 13559 packet.
    realm, baseline, created = request.getfixturevalue('same_alias_realm')
    before = copy.deepcopy((realm.head, realm.documents, realm.entries))
    captures = []
    prepared_values = []

    def capture(port, value):
        packet = copy.deepcopy(value)
        (tmp_path / 'private-capture.json').write_text(json.dumps(packet, allow_nan=False))
        captures.append(packet)

    method = DetachedPort.preview if dry_run else DetachedPort.accept

    def finish(port, prepared):
        # Retain complete values before applying Runtime's exact comparison.
        (tmp_path / 'prepared.json').write_text(json.dumps(prepared, allow_nan=False))
        prepared_values.append(copy.deepcopy(prepared))
        packet, = captures
        assert set(packet) == {'schema_version', 'context', 'original', 'landing',
                               'replay_bindings', 'prepared_digest'}
        assert packet['prepared_digest'] == _digest(prepared)
        assert packet['landing']['records'] == prepared['records']
        assert prepared['canonical_document'] == baseline
        for execution in (packet['landing'], packet['original']):
            command, = execution['commands']
            assert command['input'] == created['data']['canonical_document']
            bundle_from_document(command['output'])
            assert command['output'] == prepared['canonical_document']
        return method(port, prepared)

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', capture, raising=False)
    monkeypatch.setattr(DetachedPort, 'preview' if dry_run else 'accept', finish)
    result = submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    assert len(captures) == len(prepared_values) == 1
    assert result['data']['canonical_document'] == baseline
    if dry_run:
        assert result['receipt'] is None
        assert (realm.head, realm.documents, realm.entries) == before
    else:
        assert result['receipt']['result'] == result['data']


def test_same_alias_final_projection_requires_selected_accepted_records(request):
    realm, baseline, created = request.getfixturevalue('same_alias_realm')
    records = accepted_records(realm, realm.entries[-1])
    sources = [source for entry in records.entries for source in records.load_boundary(entry)]
    plan = execute(bundle_from_document(created['data']['canonical_document']),
                   [undo(created['receipt']['receipt_id'])], inverse_records=records,
                   commit_request={'reads': []}, evaluated_head=copy.deepcopy(realm.head))
    assert document_from_bundle(plan.bundle, source_bundles=sources,
                                inverse_records=records) == baseline
    # Historical closures or an unbound list of recorder rows cannot grant the
    # manifest-removal authority that the final production compiler requires.
    with pytest.raises(CommandError, match='media inverse needs accepted boundaries'):
        document_from_bundle(plan.bundle, source_bundles=sources, inverse_records=list(records))


def test_grouped_inverse_keeps_intermediate_media_and_forwards_exact_authority(request, monkeypatch):
    from astrid.sdk.timeline import commit

    realm, baseline, created = request.getfixturevalue('same_alias_realm')
    note = submit(realm, patch(change(['name'], present(baseline['parent_revision']['payload']['name']),
                                    'Annotated')), reads=[])
    projected = []
    original_document = commit.document_from_bundle
    original_after = CommitPatch.after

    def document(bundle, **kwargs):
        result = original_document(bundle, **kwargs)
        projected.append((kwargs.get('inverse_records'), result))
        return result

    authorities = []

    def after(metadata, ctx, *, inverse_records=()):
        start = len(projected)
        original_after(metadata, ctx, inverse_records=inverse_records)
        if not hasattr(ctx, 'inverse_stages'):
            return
        assert isinstance(inverse_records, AcceptedRecords)
        assert metadata.inverse_records[ctx.record['ordinal']] is inverse_records
        stages = metadata.inverse_stages[ctx.record['ordinal']]
        assert len(stages) == 2
        intermediate = stages[0][1].document
        assert intermediate['dependency_manifest']['media'] == created['data'][
            'canonical_document']['dependency_manifest']['media']
        assert intermediate['parent_revision']['payload']['name'] == baseline['parent_revision']['payload']['name']
        assert stages[-1][1].document == baseline
        # Completed stage and private output use the very object supplied by execute.
        completed = [output for authority, output in projected[start:] if authority is inverse_records]
        assert completed == [baseline, baseline]
        authorities.append(inverse_records)

    monkeypatch.setattr(commit, 'document_from_bundle', document)
    monkeypatch.setattr(CommitPatch, 'after', after)
    result = submit(realm, inverse([created['receipt']['receipt_id'], note['receipt']['receipt_id']]), reads=[])
    assert result['data']['canonical_document'] == baseline
    assert len(authorities) == 1
    assert sum(authority is authorities[0] for authority, _ in projected) >= 4


@pytest.mark.parametrize('damage', ['unbound', 'forged'])
def test_completed_projection_rejects_damaged_forwarded_authority(request, monkeypatch, damage):
    realm, _, created = request.getfixturevalue('same_alias_realm')
    original = CommitPatch.after

    def after(metadata, ctx, *, inverse_records=()):
        if hasattr(ctx, 'inverse_stages'):
            rows = copy.deepcopy(list(inverse_records))
            if damage == 'unbound':
                inverse_records = rows
            else:
                rows[0]['writes'] = []
                inverse_records = AcceptedRecords(inverse_records.entries, rows, inverse_records.load_boundary)
        return original(metadata, ctx, inverse_records=inverse_records)

    monkeypatch.setattr(CommitPatch, 'after', after)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(created['receipt']['receipt_id']), reads=[])
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before


@pytest.mark.parametrize('dry_run', [False, True])
def test_runtime_rejects_corrupt_completed_inverse_carrier(request, monkeypatch, dry_run):
    realm, _, created = request.getfixturevalue('same_alias_realm')
    captures = []

    def capture(port, value):
        packet = detached(value)
        packet['landing']['commands'][-1]['output']['parent_revision']['revision_id'] = 'foreign-parent'
        captures.append(packet)

    def finish(port, prepared):
        packet, = captures
        source = created['data']['canonical_document']
        # The detached ABI fixture replaces the package; Runtime's real schema
        # validator needs its resource loader for this comparison.
        with monkeypatch.context() as runtime_modules:
            runtime_modules.setitem(sys.modules, 'runtime_protocol', runtime_protocol)
            validate_sequential_capture(port.context['request'], prepared, packet, source, source)
        pytest.fail('corrupt carrier reached acceptance')

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', capture, raising=False)
    monkeypatch.setattr(DetachedPort, 'preview' if dry_run else 'accept', finish)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(created['receipt']['receipt_id']), reads=[], dry_run=dry_run)
    assert error.value.failure['details']['reason'] == 'invalid_records'
    assert error.value.failure['message'] == 'landing execution output differs from the prepared document'
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before
