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
