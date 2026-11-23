import json
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from contextvars import copy_context
from io import StringIO
from types import SimpleNamespace

import pytest

from astrid.core.cli.domain_output import cli_print, output_stream_scope
from astrid.core.gateway.dispatch import _dispatch_product
from astrid.sdk.client import AstridClient
from astrid.sdk.contracts import DomainResult


def test_copied_context_does_not_retain_finished_dispatch_sink(monkeypatch):
    contexts = []

    def composition(*args, **kwargs):
        contexts.append(copy_context())
        return DomainResult.success({'marker': 'first'})

    client = SimpleNamespace(
        timelines=SimpleNamespace(open_composition=composition),
        projects=SimpleNamespace(list=lambda: DomainResult.success({'marker': 'second'})),
    )
    monkeypatch.setattr(AstridClient, 'open_from_launcher', lambda **kwargs: nullcontext(client))
    first, second = StringIO(), StringIO()
    with redirect_stderr(StringIO()):
        with redirect_stdout(first):
            assert _dispatch_product(['timelines', 'show', 'a', '--project', 'p', '--timings', '--json']) == 0
        original = first.getvalue()
        with redirect_stdout(second):
            assert contexts[0].run(_dispatch_product, ['projects', 'list', '--json']) == 0
    assert json.loads(second.getvalue())['data'] == {'marker': 'second'}
    assert first.getvalue() == original


def test_scope_expiration_and_nested_exception_restore():
    outer, inner, fallback = StringIO(), StringIO(), StringIO()
    with redirect_stdout(fallback):
        with output_stream_scope(outer):
            captured_outer = copy_context()
            with pytest.raises(ValueError):
                with output_stream_scope(inner):
                    captured_inner = copy_context()
                    cli_print('inner')
                    raise ValueError('stop')
            cli_print('outer')
            captured_inner.run(cli_print, 'expired inner')
        captured_outer.run(cli_print, 'expired outer')
    assert inner.getvalue() == 'inner\n'
    assert outer.getvalue() == 'outer\n'
    assert fallback.getvalue() == 'expired inner\nexpired outer\n'


def test_dispatch_startup_failure_uses_its_own_default_stdout(monkeypatch):
    from astrid.sdk.exceptions import ServiceUnavailableError

    def unavailable(**kwargs):
        raise ServiceUnavailableError('unavailable')

    monkeypatch.setattr(AstridClient, 'open_from_launcher', unavailable)
    outer, current = StringIO(), StringIO()
    with output_stream_scope(outer), redirect_stdout(current):
        assert _dispatch_product(['projects', 'list', '--json']) == 1
    assert json.loads(current.getvalue())['error']['code'] == 'unavailable'
    assert outer.getvalue() == ''
