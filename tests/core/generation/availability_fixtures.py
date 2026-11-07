"""Availability declarations the tests supply themselves.

Release qualification is declared by installed pack manifests, with no
central fallback. A test that needs an unqualified Wan route declares it here.
"""

from __future__ import annotations

from types import MappingProxyType

WAN_UNQUALIFIED = MappingProxyType({
    'actions': {},
    'fixture_only_routes': {},
    'models': {
        'wan-2.2': {'available': False, 'error': {
            'code': 'generation_route_unqualified',
            'message': 'wan-2.2 generation is unavailable because this model has not been qualified'}},
        'wan-templates': {'available': False, 'error': {
            'code': 'generation_route_unqualified',
            'message': 'Wan generation templates are unavailable because they have not been qualified'}},
    },
    'template_models': {'video/wan': 'wan-templates'},
})


def declare_wan_unqualified(monkeypatch) -> None:
    from astrid.core.generation import availability, listing
    for module in (availability, listing):
        monkeypatch.setattr(module, 'availability_declaration', lambda *args, **kwargs: WAN_UNQUALIFIED)


UNQUALIFIED_ACTIONS = ('wan2gp.generate_video', 'video_editing.animate_image', 'vibecomfy.character_animation')


def _unqualified_action(action_id: str) -> dict:
    return {'available': False, 'error': {
        'code': 'generation_route_unqualified',
        'message': f'{action_id} is unavailable because this generation route has not been qualified'}}


def declaration_with_actions(base=None, extra_actions=None) -> dict:
    """Wan models plus the unqualified Wan actions and an unimplemented action,
    layered over the declarations the real installed pack manifests supply."""
    from astrid.core.generation import availability
    live = availability.availability_declaration()
    merged = {key: dict(live.get(key, {})) for key in ('actions', 'fixture_only_routes', 'models', 'template_models')}
    merged['schema_version'] = 1
    for key in ('models', 'template_models'):
        merged[key].update(WAN_UNQUALIFIED[key])
    for action_id in UNQUALIFIED_ACTIONS:
        merged['actions'][action_id] = _unqualified_action(action_id)
    merged['actions']['video_editing.thumbnail_maker'] = {'available': False, 'error': {
        'code': 'generation_action_unavailable',
        'message': 'video_editing.thumbnail_maker generation is not implemented'}}
    merged['actions'].update(extra_actions or {})
    return merged


def declare_wan_actions_unqualified(monkeypatch, extra_actions=None) -> dict:
    from astrid.core.generation import availability, listing
    declaration = declaration_with_actions(extra_actions=extra_actions)
    for module in (availability, listing):
        monkeypatch.setattr(module, 'availability_declaration', lambda *args, **kwargs: declaration)
    return declaration
