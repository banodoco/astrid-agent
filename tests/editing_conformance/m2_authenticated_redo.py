"""Authenticated redo boundaries for the current M2 recorder fixtures."""
from __future__ import annotations

import copy
from typing import Any


class AuthenticatedRedo:
    """Keep local fixture execution while sourcing redo history from the provider."""

    def __init__(self, execute, inverse):
        self._execute = execute
        self._inverse = inverse
        self._contexts: dict[int, tuple[Any, Any, Any]] = {}

    def execute(self, bundle, commands, **kwargs):
        plan = self._execute(bundle, commands, **kwargs)
        self._contexts[id(plan.records)] = (
            plan.records,
            copy.deepcopy(bundle),
            (copy.deepcopy(commands), copy.deepcopy(kwargs.get('verified_media', ()))),
        )
        return plan

    def inverse(self, bundle, records, *args, **kwargs):
        result = self._inverse(bundle, records, *args, **kwargs)
        context = self._contexts.get(id(records))
        if context is not None:
            self._contexts[id(result.records)] = context
        return result

    def accepted_boundaries(self, _records, before, after):
        return self._accepted(_records, before, after)

    def accepted(self, records, before, after):
        return self._accepted(records, before, after)

    def _accepted(self, records, before, after):
        from astrid.sdk.timeline._collections import AcceptedRecords
        from astrid.sdk.timeline._commit_patch import inverse_address_resolver
        from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
        from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm
        from tests.sdk.test_timeline_commit_provider import inverse as inverse_call

        context = self._contexts.get(id(records))
        if context is None:
            raise AssertionError('redo boundary has no captured production execution')
        _, original_bundle, (commands, verified_media) = context
        document = document_from_bundle(original_bundle)
        realm = MediaRealm(document, verified_media)
        created = realm.call(commands)
        if records and records[0]['command']['op'] == 'timeline.inverse':
            realm.call([inverse_call([created['receipt']['receipt_id']])])
        entry = copy.deepcopy(realm.entries[-1])
        history = {item['publication_id']: copy.deepcopy(item) for item in realm.entries}

        def proof_boundary(item):
            previous = history[item['previous']['publication_id']]
            previous_document = previous['receipt']['result'].get('canonical_document')
            if previous_document is None:
                previous_document = realm.documents[previous['revision_id']]
            current_document = item['receipt']['result'].get('canonical_document')
            if current_document is None:
                current_document = realm.documents[item['revision_id']]
            return (
                bundle_from_document(previous_document),
                bundle_from_document(current_document),
            )

        accepted = AcceptedRecords([entry], entry['records']['commands'], proof_boundary)
        self._contexts[id(accepted)] = context
        accepted.address_resolver = inverse_address_resolver(
            [entry], after, load_boundary=proof_boundary, load_history=history.__getitem__)
        authenticated = self._inverse(after, accepted)
        if '_doc_patch_images' in authenticated.bundle.get('source_mapping', {}):
            images = authenticated.bundle['source_mapping']['_doc_patch_images']
            for bundle in (before, after):
                bundle['source_mapping']['_doc_patch_images'] = copy.deepcopy(images)
        return accepted
