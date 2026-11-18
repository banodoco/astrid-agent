"""Lead review 6962279830: functional probes against the 0a49ce48 input."""
from __future__ import annotations

import asyncio
import select
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.comfy_held import ComfyTargetSession
from astrid.core.generation.backends.comfy_target import ComfyTarget
from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient
from astrid.core.generation.engine_contracts import TransportError
from tests.helpers.ls5_comfy_peer import ComfyPeer


@pytest.fixture(autouse=True)
def exact_checkout_import():
    from astrid.core.generation.backends import comfy_held

    assert Path(comfy_held.__file__).resolve().parents[4] == Path(__file__).resolve().parents[4]


async def terminal(runtime, prompt):
    from vibecomfy.runtime.session import SessionConfig, _wait_for_server_history

    return await _wait_for_server_history(runtime.server_url, prompt, config=SessionConfig(), client=runtime)


def peer_closed_http(owner):
    """Wait for the real peer's short keep-alive expiry, without consuming EOF."""
    sock = owner.pool.connection.sock
    assert select.select([sock], [], [], 5)[0], "peer did not close its idle HTTP connection"
    assert sock.recv(1, socket.MSG_PEEK) == b""


@pytest.mark.parametrize("api", ["runtime", "direct"])
def test_idle_post_is_typed_not_started_and_new_client_posts_once(api):
    with ComfyPeer(keepalive_timeout=1) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        client.object_info()
        original = client.generation
        peer_closed_http(owner)
        runtime = TargetRuntimeClient(client)

        async def run():
            with pytest.raises(TransportError) as caught:
                if api == "runtime":
                    await runtime._post_prompt({})
                else:
                    client.submit_prompt({"prompt": {}})
            error = caught.value
            assert error.code == "comfy_request_not_started"
            assert error.request_started is False and error.acceptance_known is True
            assert error.prompt_not_started is True
            assert original.retired and not peer.posts
            with pytest.raises(TransportError):
                await runtime._post_prompt({})
            await runtime.release_attempt()

            replacement = owner.client()
            assert replacement.generation.identity != original.identity
            assert replacement.generation.sequence == original.sequence + 1
            assert not replacement._witnessed_prompts and not replacement._prompt_attempted
            replacement.object_info()
            fresh = TargetRuntimeClient(replacement)
            prompt = (await fresh._post_prompt({}))["prompt_id"]
            assert prompt in await terminal(fresh, prompt)
            await fresh.release_attempt()

        asyncio.run(run())
        assert peer.counts()["POST /prompt"] == 1
        assert peer.counts()["GET /object_info"] == 2
        assert peer.counts()["GET /ws"] == 2


def test_idle_http_read_recovers_running_prompt_without_reposting():
    with ComfyPeer(auto_complete=False, keepalive_timeout=1) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        runtime = TargetRuntimeClient(owner.client())
        original = runtime.client.generation

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            await asyncio.to_thread(peer_closed_http, owner)
            peer.complete(prompt)
            history = await terminal(runtime, prompt)
            assert history[prompt]["outputs"] == peer.outputs
            assert original.retired
            assert runtime.evidence["mode"] == "bounded-poll-disconnect"
            assert runtime.evidence["recovery_generation"] != original.identity
            assert "idle HTTP" in runtime.evidence["reason"]
            assert runtime._recovery.owns_prompt(prompt)
            assert runtime._recovery._prompt_attempted
            with pytest.raises(TransportError):
                await runtime._post_prompt({})
            await runtime.release_attempt()

        asyncio.run(run())
        assert peer.counts()["POST /prompt"] == 1
        assert peer.counts()["GET /history/prompt-1"] == 1


def test_live_http_connection_older_than_thirty_seconds_is_not_used(monkeypatch):
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        client.object_info()
        calls = list(peer.calls)
        now = time.monotonic()
        with monkeypatch.context() as clock:
            clock.setattr(time, "monotonic", lambda: now + 31)
            with pytest.raises(TransportError) as caught:
                client.object_info()
        assert caught.value.code == "comfy_request_not_started"
        assert caught.value.request_started is False
        assert client.generation.retired
        assert peer.calls == calls


@pytest.mark.parametrize("status", [200, 400, 403, 426, 500])
def test_websocket_rejection_before_post_uses_bounded_fallback(status):
    with ComfyPeer(delay=0) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        peer.websocket_status = status
        runtime = TargetRuntimeClient(owner.client())
        assert runtime.evidence["mode"] == "bounded-poll-incompatible"
        assert runtime.evidence["reason"]
        assert owner.generation.retired

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            assert await runtime.history_ready(prompt) is False
            assert prompt in await terminal(runtime, prompt)
            await runtime.release_attempt()

        asyncio.run(run())
        assert len(peer.posts) == 1 and "client_id" not in peer.posts[0]
        assert peer.counts()["GET /ws"] == 1


@pytest.mark.parametrize("first_status", [None, "unexpected"])
def test_missing_or_invalid_initial_status_uses_bounded_fallback(first_status):
    with ComfyPeer(delay=0) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        peer.first_status = first_status
        started = time.monotonic()
        runtime = TargetRuntimeClient(owner.client())
        assert time.monotonic() - started < 20
        assert runtime.evidence["mode"] == "bounded-poll-incompatible"
        assert owner.generation.retired

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            assert prompt in await terminal(runtime, prompt)
            await runtime.release_attempt()

        asyncio.run(run())
        assert len(peer.posts) == 1 and "client_id" not in peer.posts[0]
        assert not peer.sockets


def test_http_connect_failure_keeps_existing_object_info_error():
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{unavailable.getsockname()[1]}"
        with ComfyTargetSession(ComfyTarget(url, "attached")) as owner:
            with pytest.raises(ValueError, match=r"^comfy target /object_info request failed$"):
                owner.client()
            assert owner.generation.retired
            assert owner.fallback_reason is None


@pytest.mark.parametrize("history_state", ["empty", "pending", "queued", "running"])
def test_early_ready_history_falls_back_once_and_keeps_terminal_validation(history_state, monkeypatch):
    from vibecomfy.runtime import session

    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        runtime = TargetRuntimeClient(owner.client())
        sleeps = []

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            if history_state != "empty":
                peer.history[prompt] = {"status": {"status_str": history_state, "completed": False}, "outputs": {}}
            peer.event(prompt, "executing", node=None)

            async def finish_on_poll(delay):
                sleeps.append(delay)
                peer.complete(prompt)

            # Exactly one existing bounded poll is required; no new polling loop.
            proxy = SimpleNamespace(**vars(asyncio))
            proxy.sleep = finish_on_poll
            monkeypatch.setattr(session, "asyncio", proxy)
            history = await terminal(runtime, prompt)
            assert history[prompt]["outputs"] == peer.outputs
            assert runtime.evidence["mode"] == "bounded-poll-early-ready"
            assert runtime.evidence["reason"]
            assert not runtime.client.generation.retired
            assert sleeps == [1]
            await runtime.release_attempt()

        asyncio.run(run())
        assert peer.counts()["POST /prompt"] == 1
        assert peer.counts()["GET /history/prompt-1"] == 2
        assert peer.counts()["GET /ws"] == 1


def test_stale_post_reprepares_with_fresh_target_facts_before_sending(monkeypatch, tmp_path):
    from astrid.packs.vibecomfy import local_server_engine

    generations = []
    with ComfyPeer(keepalive_timeout=1) as peer:
        def prepare(_resolved, _destination, *, session, **_kwargs):
            client = session.client()
            generations.append(client.generation.identity)
            client.object_info()
            if len(generations) == 1:
                peer_closed_http(session)
            runtime = TargetRuntimeClient(client)

            async def run():
                try:
                    prompt = (await runtime._post_prompt({}))["prompt_id"]
                    return await terminal(runtime, prompt)
                except Exception as exc:
                    # The real Vibe runner retains the transport error as its cause.
                    raise RuntimeError("queue wrapper") from exc
                finally:
                    await runtime.release_attempt()

            return asyncio.run(run())

        monkeypatch.setattr(local_server_engine, "_run_local_server", prepare)
        result = local_server_engine.run_local_server(
            object(), tmp_path, comfy_url=peer.url, comfy_output_dir="",
            task_identity="task", attempt_identity="attempt", run_inputs=None)
        assert result["prompt-1"]["outputs"] == peer.outputs
        assert len(generations) == len(set(generations)) == 2
        assert peer.counts()["GET /object_info"] == 2
        assert peer.counts()["POST /prompt"] == 1
        assert peer.counts()["GET /ws"] == 2


@pytest.mark.parametrize("failure", ["lost-ack", "read-after-post", "always-stale"])
def test_repreparation_is_bounded_and_never_repeats_a_started_prompt(failure, monkeypatch, tmp_path):
    from astrid.packs.vibecomfy import local_server_engine

    preparations = []
    with ComfyPeer(auto_complete=False, keepalive_timeout=1) as peer:
        if failure == "lost-ack":
            peer.mode = failure

        def prepare(_resolved, _destination, *, session, **_kwargs):
            client = session.client()
            preparations.append(client.generation.identity)
            client.object_info()
            if failure == "always-stale":
                peer_closed_http(session)
            runtime = TargetRuntimeClient(client)

            async def run():
                try:
                    prompt = (await runtime._post_prompt({}))["prompt_id"]
                    assert failure == "read-after-post"
                    await asyncio.to_thread(peer_closed_http, session)
                    client.history(prompt)
                    pytest.fail("retired HTTP read unexpectedly succeeded")
                except Exception as exc:
                    raise RuntimeError("failed operation") from exc
                finally:
                    await runtime.release_attempt()

            return asyncio.run(run())

        monkeypatch.setattr(local_server_engine, "_run_local_server", prepare)
        with pytest.raises(RuntimeError, match="failed operation"):
            local_server_engine.run_local_server(
                object(), tmp_path, comfy_url=peer.url, comfy_output_dir="",
                task_identity="task", attempt_identity="attempt", run_inputs=None)
        assert len(preparations) == (2 if failure == "always-stale" else 1)
        assert peer.counts()["POST /prompt"] == (0 if failure == "always-stale" else 1)
        assert peer.counts()["GET /history/prompt-1"] == 0


@pytest.mark.parametrize("defect", ["other-prompt", "missing-status", "missing-outputs"])
def test_early_ready_fallback_does_not_accept_malformed_history(defect):
    from vibecomfy.errors import QueueError

    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        runtime = TargetRuntimeClient(owner.client())

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            if defect == "other-prompt":
                # Keep the selected route, but return another prompt's entry.
                original = runtime._attempt.history

                async def wrong_prompt(value):
                    await original(value)
                    return {"not-ours": {"status": peer.status, "outputs": peer.outputs}}

                runtime._attempt.history = wrong_prompt
            elif defect == "missing-status":
                peer.history[prompt] = {"outputs": peer.outputs}
            else:
                peer.history[prompt] = {"status": peer.status}
            peer.event(prompt, "executing", node=None)
            with pytest.raises(QueueError, match="Malformed Comfy terminal result"):
                await terminal(runtime, prompt)
            assert runtime.evidence["mode"] == "held"
            await runtime.release_attempt()

        asyncio.run(run())
        assert peer.counts()["POST /prompt"] == 1
        assert peer.counts()["GET /history/prompt-1"] == 1


def test_agent_declares_bounded_websockets_dependency():
    import tomllib

    from packaging.requirements import Requirement

    project = Path(__file__).resolve().parents[4] / "pyproject.toml"
    dependencies = tomllib.loads(project.read_text())["project"]["dependencies"]
    requirements = [Requirement(value) for value in dependencies]
    websocket = next((value for value in requirements if value.name == "websockets"), None)
    assert websocket is not None, "Agent directly uses websockets and must declare its compatible range"
    assert any(part.operator in {"<", "<="} for part in websocket.specifier)
    assert websocket.specifier.contains("16.1.1")  # Existing managed lock remains supported.
