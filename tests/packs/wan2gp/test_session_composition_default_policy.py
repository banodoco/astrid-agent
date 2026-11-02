"""M2-11a review 88 X-5: the registered wan profile composes on the default policy.

Production registry, no test profile, the real invoke_wan_session and the real
managed-session admission. Only the native child edge (_ManagedWanChildAdapter.invoke)
and the child startup are faked. Exactly one admission dispatch must happen.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from astrid.core.execution.generic_host import GenericPackHost, _ManagedWanChildAdapter
from astrid.core.execution.managed_tool_session import ManagedToolSession
from tests.packs.wan2gp import test_session_run_task_parity as parity


def _real_type_prepare(host: GenericPackHost):
    """Child startup is faked, but the admitted adapter is the real wan adapter type."""
    from astrid.core.execution.managed_tool_session import CapabilityDescriptor, SessionBinding

    def prepare_wan_session(*, owner_dir: Any, root: Any, python: Any, source_digest: str, config_digest: str,
                            readiness_timeout: float, release_timeout: float, init_options: Any = None) -> SessionBinding:
        binding = SessionBinding(
            session_id=str(owner_dir), runtime_instance_id="parity-runtime-instance",
            process_birth_id="parity-birth", endpoint="pipe:parity-birth",
            source_digest=source_digest, config_digest=config_digest,
            execution_identity="parity-identity", launch_generation="parity-birth",
            engine_birth_id="parity-birth",
        )
        # The real constructor (it starts no process); the native invoke is faked at class level.
        adapter = _ManagedWanChildAdapter(
            spec={}, binding=binding, readiness_timeout=1.0, release_timeout=1.0,
            track=lambda _process: None, untrack=lambda _process: None)
        adapter.ready = True
        host.managed_tool_session.open(
            capability=CapabilityDescriptor("wan2gp.generate_video", warm_reuse_expected=True),
            binding=binding, adapter=adapter,
        )
        return binding

    return prepare_wan_session


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[dict[str, Any]], int]:
    import astrid.core.generation.availability as availability
    from astrid.core.pack.loader import _default_pack_roots

    monkeypatch.setattr(availability, "action_availability", lambda _route: {"available": True})
    client = parity._RuntimeClient()
    host = GenericPackHost(pack_roots=_default_pack_roots(), client=client)
    host.discover()
    record = host.capabilities["wan2gp.generate_video"]
    host.capabilities["wan2gp.generate_video"] = dataclasses.replace(record, ready=True)

    calls: list[dict[str, Any]] = []
    edge = parity._engine_edge(tmp_path / "native", calls)
    # Only the native child edge is faked. The host's own invoke_wan_session and the
    # registered profile are the real ones.
    monkeypatch.setattr(_ManagedWanChildAdapter, "invoke",
                        lambda self, token, settings, **kw: edge(token, settings, **kw))
    # Child lifecycle edges (warm-slot observe, release, cancel) are faked the same way.
    for name in ("observe", "release", "cancel"):
        monkeypatch.setattr(_ManagedWanChildAdapter, name, lambda self, **_kw: {"ok": True})
    monkeypatch.setattr(host, "prepare_wan_session", _real_type_prepare(host))
    monkeypatch.setattr(host, "preflight", lambda *_: None)

    dispatches: list[object] = []
    real_adapter_for = ManagedToolSession.adapter_for

    def counting_adapter_for(self, token, *, begin: bool = False):
        adapter = real_adapter_for(self, token, begin=begin)
        if begin:
            dispatches.append(token)
        return adapter

    monkeypatch.setattr(ManagedToolSession, "adapter_for", counting_adapter_for)

    task = parity._task(tmp_path, dict(parity.EXPLICIT_INPUTS))
    client.tasks["wan-task"] = task
    host.run_task(task, lease_token="lease")
    return client, calls, len(dispatches)


def test_registered_wan_profile_composes_with_exactly_one_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls, dispatch_count = _run(tmp_path, monkeypatch)
    assert dispatch_count == 1, dispatch_count
    assert len(calls) == 1
    assert len(client.settlements) == 1
    assert client.failures == []
