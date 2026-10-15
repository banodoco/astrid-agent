"""Compatibility entry points into the single timing compiler.

Read-only lookup and declaration helpers stay available on Checkout. Methods
that materialize timing run the existing evaluators on a private compiler copy.
"""
from functools import wraps

from astrid.sdk.timeline._timing_evaluators import (
    _TimingEvaluators, _bar_len, _bed_clusters, _is_seconds,
)


# Existing SDK calls, not an additional EditCommand/OpCall language.
# Private entries are used by Clip, StepSequence, retime_scope, save and publish.
COMPILED_METHODS = (
    "declare_gaps", "reflow", "insert_line", "remove_line", "apply_script",
    "retime", "resolve", "film_end", "_sync_film_length", "_place_on",
    "_shift_after", "_relay_steps", "_apply_bed_cuts",
)


def _compiled(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        from astrid.sdk.timeline.compiler import _compile_checkout

        return _compile_checkout(self, method.__name__, args, kwargs)
    call._timing_entrypoint = True
    return call


class _ReflowMixin(_TimingEvaluators):
    """Preserve existing signatures and return values while compiling once."""


for _name in COMPILED_METHODS:
    setattr(_ReflowMixin, _name, _compiled(getattr(_TimingEvaluators, _name)))
