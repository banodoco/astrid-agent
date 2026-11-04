"""Server-only replay of the original captured intent on an accepted head."""
from __future__ import annotations

from ._command_state import State
from .commands import execute
from .inverse import check_intervening


def replay(plan, head_bundle, *, intervening_records=(), inverse_records=(), id_map=None, closures=None,
           verified_media=(), schema_inputs=(), realm_id=None, resolve_inputs=None,
           verified_producer=None, commit_request=None, evaluated_head=None,
           replay_bindings=None):
    check_intervening(plan.records, intervening_records, state=State(head_bundle),
                      identities_only=all(command['op'] not in ('timeline.inverse', 'timeline.restore')
                                          for command in plan.commands))
    return execute(head_bundle, plan.commands, inverse_records=inverse_records,
                   intervening_records=intervening_records, id_map=id_map, closures=closures,
                   verified_media=verified_media, schema_inputs=schema_inputs, realm_id=realm_id,
                   resolve_inputs=resolve_inputs, verified_producer=verified_producer,
                   commit_request=commit_request, evaluated_head=evaluated_head,
                   replay_bindings=replay_bindings)
