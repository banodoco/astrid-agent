"""Embedded commands have their own discovery lifetime inside an outer scope."""

import pytest

from astrid import console
from astrid.core import gateway
from astrid.core.pack import discovery


@pytest.mark.parametrize("entrypoint", ["console", "gateway"])
@pytest.mark.parametrize("fail", [False, True])
def test_nested_commands_revoke_their_owners_and_restore_outer(monkeypatch, entrypoint, fail):
    owners = []

    def command(*args, **kwargs):
        owner = discovery._active_discovery_cache()
        assert owner is not None and owner is not outer
        owners.append(owner)
        owner["command"] = len(owners)
        if fail:
            raise RuntimeError("command failed")
        return 0

    if entrypoint == "console":
        monkeypatch.setattr(console, "_run_console_impl", command)
        invoke = lambda: console.main([])
    else:
        monkeypatch.setattr(gateway, "_main_impl", command)
        monkeypatch.setattr(gateway, "render_astrid_error", lambda error: 9)
        invoke = lambda: gateway.main([])

    with discovery.pack_discovery_scope():
        outer = discovery._active_discovery_cache()
        outer["outer"] = "preserved"
        for _ in range(2):
            if fail and entrypoint == "console":
                with pytest.raises(RuntimeError, match="command failed"):
                    invoke()
            else:
                assert invoke() == (9 if fail else 0)
            assert discovery._active_discovery_cache() is outer
            assert outer.active and outer == {"outer": "preserved"}
            assert not owners[-1].active and owners[-1] == {}
        assert owners[0] is not owners[1]


def test_console_gateway_handoff_and_nested_gateway_have_correct_owners(monkeypatch):
    observed = {}
    real_console = console._run_console_impl

    def capture_console(*args, **kwargs):
        observed["console"] = discovery._active_discovery_cache()
        return real_console(*args, **kwargs)

    def gateway_command(args):
        if args == ["nested"]:
            observed["nested"] = discovery._active_discovery_cache()
            return 0
        observed["gateway"] = discovery._active_discovery_cache()
        observed["nested_result"] = gateway.main(["nested"])
        observed["after_nested"] = discovery._active_discovery_cache()
        return 0

    monkeypatch.setattr(console, "_run_console_impl", capture_console)
    monkeypatch.setattr(gateway, "_main_impl", gateway_command)
    with discovery.pack_discovery_scope():
        outer = discovery._active_discovery_cache()
        outer["outer"] = "preserved"
        assert console.main(["generate", "example"]) == 0
        assert observed["nested_result"] == 0
        assert observed["console"] is observed["gateway"] is observed["after_nested"]
        assert observed["console"] is not outer
        assert observed["nested"] is not outer
        assert observed["nested"] is not observed["console"]
        assert not observed["nested"].active
        assert not observed["console"].active
        assert discovery._active_discovery_cache() is outer
        assert outer.active and outer == {"outer": "preserved"}
