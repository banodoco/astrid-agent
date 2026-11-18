"""An incomplete catalog association blocks only when a change introduces or touches it."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.native_media_admission import (
    check_registry_associations, registry_selections, validate_admissions)
from astrid.sdk.timeline.publish import _media_admission_inputs

HALF = {'media_id': 'sha256:' + '4a' * 32, 'generationId': 'gen-1', 'variantId': None}


def _bundle(**assets):
    return {'parent': {'registry': {'assets': copy.deepcopy(assets)}, 'config': {}}, 'shots': {}}


def test_reading_an_incomplete_entry_never_raises_and_yields_nothing():
    assert list(registry_selections(_bundle(a=HALF))) == []
    complete = {**HALF, 'variantId': 'var-1'}
    assert list(registry_selections(_bundle(a=complete))) == [
        {'asset': 'a', 'generation_id': 'gen-1', 'variant_id': 'var-1', 'object_id': HALF['media_id']}]


def test_unchanged_half_entry_is_preexisting_and_does_not_block():
    base = _bundle(a=HALF)
    candidate = _bundle(a=HALF, other={'media_id': 'sha256:' + 'bb' * 32})
    check_registry_associations(candidate, base)


def test_new_half_entry_refuses():
    with pytest.raises(CommandError, match='requires both generation and variant'):
        check_registry_associations(_bundle(a=HALF), _bundle())


def test_changed_half_entry_refuses():
    changed = {**HALF, 'generationId': 'gen-2'}
    with pytest.raises(CommandError, match='requires both generation and variant'):
        check_registry_associations(_bundle(a=changed), _bundle(a=HALF))
    with pytest.raises(CommandError):
        check_registry_associations(_bundle(a={**HALF, 'generationId': None, 'variantId': 'v'}),
                                    _bundle(a=HALF))


def test_half_entry_in_a_shot_scope_is_keyed_by_shot():
    def with_shot(entry):
        bundle = _bundle()
        bundle['shots'] = {'s1': {'internal_timeline': {'registry': {'assets': {'a': entry}}}}}
        return bundle

    check_registry_associations(with_shot(HALF), with_shot(HALF))
    with pytest.raises(CommandError):
        check_registry_associations(with_shot(HALF), _bundle(a=HALF))


def test_publish_with_unchanged_half_entry_in_the_base_is_not_blocked(monkeypatch):
    from astrid.core.timeline import authoring_bundle

    monkeypatch.setattr(authoring_bundle, 'compile_authoring_candidate',
                        lambda candidate: type('C', (), {'publication': {'dependency_manifest': {'media': []}}})())
    monkeypatch.setattr('astrid.sdk.timeline.commit.bundle_from_document', lambda document: _bundle(a=HALF))
    base = {'source_mapping': {'_runtime_document': {'dependency_manifest': {'media': []}}}}
    assert _media_admission_inputs(base, _bundle(a=HALF), client=None) == []
    with pytest.raises(CommandError):
        _media_admission_inputs(base, _bundle(a={**HALF, 'generationId': 'gen-2'}), client=None)


def test_clip_add_media_style_admission_ignores_unchanged_half_entry(monkeypatch):
    monkeypatch.setattr('astrid.sdk.timeline.commit.bundle_from_document', lambda document: _bundle(a=HALF))
    pin = {'media_id': 'sha256:' + 'cc' * 32, 'content_digest': 'sha256:' + 'cc' * 32}
    monkeypatch.setattr('astrid.core.timeline.authoring_bundle.compile_authoring_candidate',
                        lambda bundle: type('C', (), {'publication': {'dependency_manifest': {'media': [pin]}}})())
    bundle = {**_bundle(a=HALF), 'source_mapping': {'_runtime_document': {}}}
    validate_admissions(bundle, [{'op': 'media.admit', 'args': {'inputs': [{'pin': pin}]}}])
    bundle['parent']['registry']['assets']['new'] = {**HALF, 'generationId': 'gen-9'}
    with pytest.raises(CommandError):
        validate_admissions(bundle, [{'op': 'media.admit', 'args': {'inputs': [{'pin': pin}]}}])
