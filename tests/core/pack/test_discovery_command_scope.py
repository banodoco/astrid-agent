"""Command entry points own and revoke their discovery scopes."""

import pytest

from astrid import console
from astrid.core import gateway
from astrid.core.pack import discovery


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
def test_each_command_owns_a_fresh_scope(monkeypatch, entrypoint):
    observed = []

    def capture(*args, **kwargs):
        cache = discovery._active_discovery_cache()
        assert cache is not None
        with discovery.pack_discovery_scope():
            assert discovery._active_discovery_cache() is cache
        observed.append(cache)
        return 0

    if entrypoint == "console":
        monkeypatch.setattr(console, "_run_console_impl", capture)
        invoke = lambda: console._run_console([], start_provenance="command entry")
    else:
        monkeypatch.setattr(gateway, "_main_impl", capture)
        invoke = lambda: gateway.main([])

    assert discovery._active_discovery_cache() is None
    assert invoke() == 0
    assert discovery._active_discovery_cache() is None
    assert not observed[0].active
    assert invoke() == 0
    assert discovery._active_discovery_cache() is None
    assert not observed[1].active
    assert observed[0] is not observed[1]
