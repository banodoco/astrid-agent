"""LS-5 composed real-socket contract, with a clock that forbids healthy polling."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.comfy_held import ComfyTargetSession
from astrid.core.generation.backends.comfy_target import ComfyTarget
from astrid.core.generation.backends.vibecomfy_local_server import (
    AttachedArtifacts,
    TargetRuntimeClient,
)
from astrid.core.generation.engine_contracts import TransportError
from tests.helpers.ls5_comfy_peer import ComfyPeer

DESCRIPTOR = {"filename": "result.png", "subfolder": "", "type": "output"}


@pytest.fixture
def no_poll_clock(monkeypatch):
    from vibecomfy.runtime import session

    def forbid(*_args, **_kwargs):
        raise AssertionError("healthy Comfy path slept or polled")

    clock = SimpleNamespace(monotonic=lambda: 100.0)
    proxy = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")})
    proxy.sleep = forbid
    monkeypatch.setattr(session, "time", clock)
    monkeypatch.setattr(session, "asyncio", proxy)
    return clock


async def terminal(runtime, prompt):
    from vibecomfy.runtime.session import SessionConfig, _wait_for_server_history

    return await _wait_for_server_history(runtime.server_url, prompt, config=SessionConfig(), client=runtime)


@pytest.mark.parametrize("outcome", ["normal", "cached", "failed", "interrupted"])
def test_history_ready_orders_and_preserves_typed_results(outcome, no_poll_clock):
    from vibecomfy.errors import RuntimeNodeError

    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        if outcome in {"failed", "interrupted"}:
            event = "execution_error" if outcome == "failed" else "execution_interrupted"
            peer.status = {"status_str": "error", "completed": False,
                           "messages": [[event, {"exception_message": "node failed"}]]}
            peer.outputs = {}
        client = owner.client()
        client.object_info()
        runtime = TargetRuntimeClient(client)

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            if outcome == "cached":
                peer.event(prompt, "execution_cached", nodes=["9"])
            elif outcome in {"failed", "interrupted"}:
                peer.event(prompt, event)
            else:
                peer.event(prompt, "executing", node="9")
                peer.event(prompt, "execution_success")
            wait = asyncio.create_task(terminal(runtime, prompt))
            # The earlier terminal-looking messages cannot initiate a history read.
            await asyncio.to_thread(lambda: None)
            assert not wait.done()
            assert not any(path.startswith("/history/") for _method, path in peer.calls)
            peer.complete(prompt)
            if outcome in {"failed", "interrupted"}:
                with pytest.raises(RuntimeNodeError):
                    await asyncio.wait_for(wait, timeout=5)
            else:
                history = await asyncio.wait_for(wait, timeout=5)
                assert history[prompt]["outputs"] == peer.outputs
            await runtime.release_attempt()
            assert peer.counts()["POST /prompt"] == 1
            assert peer.counts()[f"GET /history/{prompt}"] == 1
            assert peer.events[0][0] == "socket" and peer.events[1][0] == "submit"
            assert peer.events.index(("history", prompt)) < peer.events.index(("history_ready", prompt))
            assert not owner.generation.retired

        asyncio.run(run())
        http_connections = {connection for path, connection in peer.connections if path != "/ws"}
        assert len(http_connections) == 1


def test_two_attempts_share_only_transport_and_not_prompt_witnesses(no_poll_clock, tmp_path):
    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        first, second = owner.client(), owner.client()
        a, b = TargetRuntimeClient(first), TargetRuntimeClient(second)

        async def run():
            one = (await a._post_prompt({}))["prompt_id"]
            two = (await b._post_prompt({}))["prompt_id"]
            with pytest.raises(ValueError, match="witnessed"):
                await a.history(two)
            with pytest.raises(TransportError, match="already attempted"):
                await a._post_prompt({})
            peer.complete(two)
            peer.complete(one)
            results = await asyncio.gather(terminal(a, one), terminal(b, two))
            assert set(results[0]) == {one} and set(results[1]) == {two}
            (tmp_path / "attempt.json").write_text(json.dumps({"prompt_id": two,
                "queue_acceptance": {"status": "accepted", "prompt_id": two},
                "artifacts": [{"descriptor": DESCRIPTOR}]}))
            with pytest.raises(ValueError, match="witnessed"):
                await AttachedArtifacts(first, runtime_client=a).download_artifacts(local_root=tmp_path / "wrong")
            assert not (tmp_path / "wrong").exists()
            await a.release_attempt()
            await b.release_attempt()
        asyncio.run(run())
        assert peer.counts()["GET /ws"] == 1
        assert len({post["client_id"] for post in peer.posts}) == 1
        assert first._witnessed_prompts.isdisjoint(second._witnessed_prompts)


def test_dropped_socket_reconciles_old_prompt_and_marks_new_generation(tmp_path):
    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        original = client.generation
        runtime = TargetRuntimeClient(client)

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            peer.drop()
            assert await runtime.history_ready(prompt) is False
            assert original.retired
            with pytest.raises(TransportError, match="retired"):
                client.history(prompt)
            peer.complete(prompt)
            assert prompt in await terminal(runtime, prompt)
            (tmp_path / "attempt.json").write_text(json.dumps({"prompt_id": prompt,
                "queue_acceptance": {"status": "accepted", "prompt_id": prompt},
                "artifacts": [{"descriptor": DESCRIPTOR}]}))
            record = await AttachedArtifacts(client, runtime_client=runtime).download_artifacts(local_root=tmp_path / "out")
            assert (tmp_path / "out/output/result.png").read_bytes() == peer.output
            assert record["connection"]["mode"] == "bounded-poll-disconnect"
            assert record["connection"]["target_generation"] != record["connection"]["recovery_generation"]
            other = owner.client()
            assert other.generation.identity != original.identity
            assert other.generation.sequence == original.sequence + 1
            other.object_info()  # A new full preparation refetches target facts.
            assert not other.owns_prompt(prompt)
            await runtime.release_attempt()
        asyncio.run(run())
        assert peer.counts()["GET /ws"] == 2
        assert peer.counts()["POST /prompt"] == 1


def test_lost_acknowledgement_never_reposts():
    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        peer.mode = "lost-ack"
        runtime = TargetRuntimeClient(client)

        async def run():
            with pytest.raises(TransportError, match="acceptance unknown") as error:
                await runtime._post_prompt({})
            assert error.value.acceptance_known is False
            with pytest.raises(TransportError):
                await runtime._post_prompt({})
            assert not client._witnessed_prompts
            await runtime.release_attempt()
        asyncio.run(run())
        assert peer.counts()["POST /prompt"] == 1


def test_drop_during_post_keeps_the_accepted_witness():
    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        peer.mode = "drop-during-post"
        runtime = TargetRuntimeClient(client)

        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            assert client.owns_prompt(prompt)
            assert await runtime.history_ready(prompt) is False
            with pytest.raises(TransportError):
                await runtime._post_prompt({})
            peer.complete(prompt)
            assert prompt in await terminal(runtime, prompt)
            await runtime.release_attempt()
        asyncio.run(run())
        assert peer.counts()["POST /prompt"] == 1


@pytest.mark.parametrize("operation", ["schema", "folder_paths", "upload", "download", "cancel", "submit"])
def test_retired_generation_refuses_all_new_io(operation, tmp_path):
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        client.witness_prompt("ours")
        source, destination = tmp_path / "source.png", tmp_path / "destination.png"
        source.write_bytes(b"input")
        destination.write_bytes(b"previous")
        calls = {"schema": client.object_info,
                 "folder_paths": client.folder_paths,
                 "upload": lambda: client.upload_media(source, task_id="task"),
                 "download": lambda: client.download_output(DESCRIPTOR, destination),
                 "cancel": lambda: client.cancel_prompt("ours"),
                 "submit": lambda: client.submit_prompt({"prompt": {}})}
        client.generation.retire("test retirement")
        before = list(peer.calls)
        with pytest.raises(TransportError, match="retired"):
            calls[operation]()
        assert peer.calls == before
        assert destination.read_bytes() == b"previous"


@pytest.mark.parametrize("operation,path", [("upload", "/upload/image"), ("download", "/view"), ("cancel", "/queue")])
def test_retirement_during_io_refuses_late_success(operation, path, tmp_path):
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        client.witness_prompt("ours")
        source, destination = tmp_path / "source.png", tmp_path / "destination.png"
        source.write_bytes(b"input")
        destination.write_bytes(b"previous")
        peer.block_path = path
        call = {"upload": lambda: client.upload_media(source, task_id="task"),
                "download": lambda: client.download_output(DESCRIPTOR, destination),
                "cancel": lambda: client.cancel_prompt("ours")}[operation]
        errors = []
        def invoke():
            try:
                call()
            except Exception as error:  # noqa: BLE001 - assert the worker's exact exception below
                errors.append(error)
        worker = threading.Thread(target=invoke)
        worker.start()
        assert peer.block_entered.wait(5)
        client.generation.retire("test retirement during response")
        peer.block_release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], TransportError)
        assert destination.read_bytes() == b"previous"
        assert not list(tmp_path.glob(".checkout-download-*"))


def test_progress_consumer_cannot_hold_completion(no_poll_clock):
    entered, release = threading.Event(), threading.Event()
    seen = []
    def consume(event):
        seen.append(event)
        entered.set()
        assert release.wait(5)
    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        runtime = TargetRuntimeClient(owner.client(), on_event=consume)
        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            peer.event(prompt, "progress", value=1, max=100)
            assert await asyncio.to_thread(entered.wait, 5)
            for value in range(2, 101):
                peer.event(prompt, "progress", value=value, max=100)
            peer.complete(prompt)
            assert prompt in await asyncio.wait_for(terminal(runtime, prompt), timeout=2)
            assert len(seen) == 1
            release.set()
            await runtime.release_attempt()
        try:
            asyncio.run(run())
        finally:
            release.set()


def test_blocked_progress_consumer_cannot_hold_worker_exit():
    code = textwrap.dedent('''
        import asyncio
        import threading
        from astrid.core.generation.backends.comfy_held import ComfyTargetSession
        from astrid.core.generation.backends.comfy_target import ComfyTarget
        from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient
        from tests.helpers.ls5_comfy_peer import ComfyPeer
        from vibecomfy.runtime.session import SessionConfig, _wait_for_server_history

        entered = threading.Event()
        def blocked_consumer(event):
            entered.set()
            threading.Event().wait()

        with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
            runtime = TargetRuntimeClient(owner.client(), on_event=blocked_consumer)
            async def run():
                prompt = (await runtime._post_prompt({}))["prompt_id"]
                peer.event(prompt, "progress", value=1, max=2)
                assert await asyncio.to_thread(entered.wait, 2)
                peer.complete(prompt)
                history = await _wait_for_server_history(peer.url, prompt, config=SessionConfig(), client=runtime)
                assert prompt in history
                await runtime.release_attempt()
            asyncio.run(run())
        print("terminal-and-owner-closed", flush=True)
    ''')
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "terminal-and-owner-closed" in result.stdout


def test_only_owned_preview_events_reach_attempt_consumers(no_poll_clock):
    received = threading.Event()
    first_events, second_events = [], []

    def first_consumer(event):
        first_events.append(event)
        received.set()

    with ComfyPeer(auto_complete=False) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        first = TargetRuntimeClient(owner.client(), on_event=first_consumer)
        second = TargetRuntimeClient(owner.client(), on_event=second_events.append)

        async def run():
            one = (await first._post_prompt({}))["prompt_id"]
            two = (await second._post_prompt({}))["prompt_id"]
            peer.binary_preview(b"unowned Comfy preview")
            peer.event(one, "preview", image="owned-preview")
            assert await asyncio.to_thread(received.wait, 5)
            assert first_events == [{"type": "preview", "data": {"prompt_id": one, "image": "owned-preview"}}]
            assert second_events == []
            assert owner.transport.unattributed_preview == b"unowned Comfy preview"
            peer.complete(one)
            peer.complete(two)
            await asyncio.gather(terminal(first, one), terminal(second, two))
            await first.release_attempt()
            await second.release_attempt()

        asyncio.run(run())


def test_queued_io_is_fenced_before_it_can_start(tmp_path):
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        source = tmp_path / "input.png"
        source.write_bytes(b"input")
        peer.block_path = "/object_info"
        errors = []
        queued = threading.Event()

        def invoke(call):
            try:
                call()
            except Exception as error:  # noqa: BLE001 - assert both exact failures below
                errors.append(error)

        def upload():
            queued.set()
            client.upload_media(source, task_id="task")

        first = threading.Thread(target=invoke, args=(client.object_info,))
        second = threading.Thread(target=invoke, args=(upload,))
        first.start()
        assert peer.block_entered.wait(5)
        second.start()
        assert queued.wait(5)
        client.generation.retire("retired while upload waited for the dispatch guard")
        peer.block_release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()
        assert len(errors) == 2 and all(isinstance(error, TransportError) for error in errors)
        assert peer.counts() == {"GET /ws": 1, "GET /object_info": 1}


def test_optional_folder_endpoint_can_be_absent_on_a_live_pair():
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        assert client.folder_paths() is None
        assert not client.generation.retired
        assert client.object_info() == {}
        assert len({connection for path, connection in peer.connections if path != "/ws"}) == 1
        client.generation.retire("optional cached facts must also be fenced")
        with pytest.raises(TransportError, match="retired"):
            client.folder_paths()


@pytest.mark.parametrize("mode", ["ws-redirect", "http-redirect"])
def test_redirects_never_reach_another_route(mode):
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        if mode == "http-redirect":
            client = owner.client()
            peer.mode = mode
            with pytest.raises((ValueError, TransportError)):
                client.object_info()
        else:
            peer.mode = mode
            with pytest.raises(ValueError, match="redirect"):
                owner.client()
        assert not any(path == "/recipient" for _method, path in peer.calls)
        assert not peer.posts
        assert owner.generation.retired


def test_proxy_environment_cannot_redirect_local_pair(monkeypatch, no_poll_clock):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        runtime = TargetRuntimeClient(owner.client())
        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            assert prompt in await terminal(runtime, prompt)
            await runtime.release_attempt()
        asyncio.run(run())


def test_custom_policy_and_pod_transport_cannot_be_bypassed():
    with pytest.raises(ValueError, match="custom opener"):
        ComfyTargetSession(ComfyTarget("http://127.0.0.1:8188", "attached"), opener=lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="attested pod"):
        ComfyTargetSession(ComfyTarget("https://pod.invalid/engines/ours", "pod"))


def test_unsupported_websocket_is_explicit_bounded_fallback():
    with ComfyPeer(delay=0) as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        peer.mode = "unsupported"
        runtime = TargetRuntimeClient(owner.client())
        assert runtime.evidence["mode"] == "bounded-poll-incompatible"
        async def run():
            prompt = (await runtime._post_prompt({}))["prompt_id"]
            assert await runtime.history_ready(prompt) is False
            assert prompt in await terminal(runtime, prompt)
            await runtime.release_attempt()
        asyncio.run(run())
        assert len(peer.posts) == 1 and "client_id" not in peer.posts[0]


def test_retired_socket_owner_is_closed_before_session_returns():
    with ComfyPeer() as peer:
        owner = ComfyTargetSession(ComfyTarget(peer.url, "attached"))
        client = owner.client()
        socket = owner.transport._socket
        client.generation.retire("retired while idle")
        owner.close()
        assert socket.owner.closed
        assert socket.socket.closed
        assert not owner._thread.is_alive()


def test_cold_socket_import_does_not_launch_platform_probe(tmp_path):
    """Windows platform.system may run cmd/ver; a guarded worker cannot."""
    script = textwrap.dedent("""
        import asyncio, platform, subprocess, sys
        from astrid.core.execution.network_policy import install, NetworkPolicyError
        from astrid.core.generation.backends.comfy_held import ComfyTargetSession
        from astrid.core.generation.backends.comfy_target import ComfyTarget
        from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient

        def native_platform_probe():
            return subprocess.check_output(["cmd", "/c", "ver"], text=True)

        platform.system = native_platform_probe
        install({"network": True, "allowed_destinations": [sys.argv[1]]}, sys.argv[2])
        with ComfyTargetSession(ComfyTarget(sys.argv[1], "attached")) as owner:
            runtime = TargetRuntimeClient(owner.client())
            async def run():
                from vibecomfy.runtime.session import SessionConfig, _wait_for_server_history
                prompt = (await runtime._post_prompt({}))["prompt_id"]
                history = await _wait_for_server_history(
                    runtime.server_url, prompt, config=SessionConfig(), client=runtime)
                assert history[prompt]["outputs"]
                await runtime.release_attempt()
            asyncio.run(run())
        try:
            native_platform_probe()
        except NetworkPolicyError:
            pass
        else:
            raise AssertionError("native descendant guard was bypassed")
        print("guarded-cold-transport-completed")
    """)
    with ComfyPeer() as peer:
        result = subprocess.run(
            [sys.executable, "-c", script, peer.url, str(tmp_path / "network.json")],
            capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "guarded-cold-transport-completed" in result.stdout
        assert peer.counts()["POST /prompt"] == peer.counts()["GET /ws"] == 1


@pytest.mark.parametrize("status", [200, 404, 405, 501, 302, 401, 500, 101])
def test_http10_response_only_allows_explicit_incompatible_fallback(status):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from websockets.exceptions import InvalidHandshake

    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            calls.append(self.path)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/escape")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        with ComfyTargetSession(ComfyTarget(url, "attached")) as owner:
            if status in {200, 404, 405, 501}:
                client = owner.client()
                assert client.transport_evidence["mode"] == "bounded-poll-incompatible"
                assert client.held_session is None
            else:
                with pytest.raises((ValueError, InvalidHandshake)):
                    owner.client()
                assert owner.fallback_reason is None
            assert owner.generation.retired
        assert len(calls) == 1 and calls[0].startswith("/ws?clientId=")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
