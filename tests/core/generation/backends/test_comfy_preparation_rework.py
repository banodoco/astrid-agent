"""Lead review 6965548316: unsent preparation and websocket retirement."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from astrid.core.generation.backends.comfy_held import ComfyTargetSession
from astrid.core.generation.backends.comfy_target import ComfyRequestNotStarted, ComfyTarget
from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient
from astrid.core.generation.engine_contracts import TransportError
from tests.core.generation.backends.test_comfy_held_rework import peer_closed_http, terminal
from tests.helpers.ls5_comfy_peer import ComfyPeer


@pytest.fixture(autouse=True)
def exact_checkout_import():
    from astrid.core.generation.backends import comfy_target

    assert Path(comfy_target.__file__).resolve().parents[4] == Path(__file__).resolve().parents[4]


async def drop_and_drain(owner, peer):
    reader = owner.transport._reader
    assert reader is not None
    await asyncio.to_thread(peer.drop)
    await asyncio.wait_for(asyncio.shield(reader), timeout=5)
    assert owner.generation.retired


def drop_before_post(owner, peer):
    owner.submit(drop_and_drain(owner, peer)).result(timeout=10)


def assert_unsent(error, client, peer):
    assert isinstance(error, ComfyRequestNotStarted)
    assert error.code == "comfy_request_not_started"
    assert error.prompt_not_started is True
    assert error.request_started is False and error.acceptance_known is True
    assert error.prompt_id is None
    assert client.generation.retired
    assert not peer.posts and not client._witnessed_prompts


@pytest.mark.parametrize("expiry", ["peer-eof", "idle-age"])
@pytest.mark.parametrize("operation", ["system-stats", "upload", "head-view", "folder-paths"])
def test_stale_preparation_is_not_started_and_reprepares(operation, expiry, tmp_path, monkeypatch):
    from PIL import Image

    from astrid.packs.vibecomfy import local_server_engine

    source = tmp_path / "source.png"
    Image.new("RGB", (1, 1)).save(source)
    preparations, refused = [], []
    with ComfyPeer(keepalive_timeout=1) as peer:
        def expire(owner):
            if expiry == "peer-eof":
                peer_closed_http(owner)
            else:
                monkeypatch.setattr(owner.pool, "last_used", time.monotonic() - 31)

        def prepare(_resolved, _destination, *, session, **_kwargs):
            client = session.client()
            preparations.append(client)
            assert not client._prompt_attempted and not client._witnessed_prompts
            client.object_info()
            if len(preparations) == 1:
                if operation == "upload":
                    # Expire after HEAD, so this cell reaches the upload opener.
                    original_size = client._input_size

                    def after_head(*args):
                        result = original_size(*args)
                        expire(session)
                        return result

                    monkeypatch.setattr(client, "_input_size", after_head)
                else:
                    expire(session)
            try:
                if operation == "system-stats":
                    assert client.system_stats() == {"system": {"os": "linux"}}
                elif operation == "upload":
                    assert client.upload_media(source, task_id="task").startswith("astrid/content/sha256-")
                elif operation == "head-view":
                    assert client._input_size("source.png", "astrid/content") == ("missing", None)
                else:
                    assert client.folder_paths() is None  # The peer's ordinary 404 remains optional.
            except TransportError as exc:
                assert_unsent(exc, client, peer)
                assert not client._prompt_attempted
                refused.append(exc)
                raise
            runtime = TargetRuntimeClient(client)

            async def run():
                try:
                    prompt = (await runtime._post_prompt({}))["prompt_id"]
                    return await terminal(runtime, prompt)
                finally:
                    await runtime.release_attempt()

            return asyncio.run(run())

        monkeypatch.setattr(local_server_engine, "_run_local_server", prepare)
        result = local_server_engine.run_local_server(
            object(), tmp_path / "outputs", comfy_url=peer.url, comfy_output_dir="",
            task_identity="task", attempt_identity="attempt", run_inputs=None)
        assert result["prompt-1"]["outputs"] == peer.outputs
        assert len(refused) == 1 and len(preparations) == 2
        assert preparations[0].generation.identity != preparations[1].generation.identity
        assert preparations[1].generation.sequence == preparations[0].generation.sequence + 1
        assert peer.counts()["GET /object_info"] == 2
        assert peer.counts()["GET /ws"] == 2
        assert peer.counts()["POST /prompt"] == 1
        if operation == "upload":
            assert peer.counts()["POST /upload/image"] == 1
        if operation == "folder-paths":
            assert peer.counts()["GET /internal/folder_paths"] == 1


@pytest.mark.parametrize("api,point", [
    ("runtime", "before-begin"), ("direct", "before-begin"),
    ("runtime", "after-begin"), ("direct", "after-begin"),
    ("runtime", "before-dispatch"), ("direct", "before-dispatch"),
    ("runtime", "after-attempt"),
])
def test_websocket_drop_before_post_is_not_started_and_reprepares(api, point, tmp_path, monkeypatch):
    from astrid.packs.vibecomfy import local_server_engine

    preparations, refused = [], []
    with ComfyPeer() as peer:
        def prepare(_resolved, _destination, *, session, **_kwargs):
            client = session.client()
            preparations.append(client)
            assert not client._prompt_attempted and not client._witnessed_prompts
            client.object_info()
            if len(preparations) == 1:
                if point == "before-begin":
                    drop_before_post(session, peer)
                elif point == "after-begin":
                    original_begin = client._begin_prompt

                    def begin(payload):
                        body = original_begin(payload)
                        drop_before_post(session, peer)
                        return body

                    monkeypatch.setattr(client, "_begin_prompt", begin)
                elif point == "before-dispatch":
                    original_open = session.pool.open

                    def open_request(request, *, timeout):
                        assert request.get_method() == "POST" and request.full_url.endswith("/prompt")
                        drop_before_post(session, peer)
                        return original_open(request, timeout=timeout)

                    monkeypatch.setattr(session.pool, "open", open_request)
                    monkeypatch.setattr(client, "_opener", open_request)
                else:
                    original_attempt = session.attempt

                    async def attempt(selected):
                        result = await original_attempt(selected)
                        if selected is client:
                            await drop_and_drain(session, peer)
                        return result

                    monkeypatch.setattr(session, "attempt", attempt)
            runtime = TargetRuntimeClient(client)

            async def run():
                try:
                    if api == "runtime":
                        prompt = (await runtime._post_prompt({}))["prompt_id"]
                        return await terminal(runtime, prompt)
                    return client.submit_prompt({"prompt": {}})
                except TransportError as exc:
                    assert_unsent(exc, client, peer)
                    refused.append(exc)
                    raise
                finally:
                    await runtime.release_attempt()

            return asyncio.run(run())

        monkeypatch.setattr(local_server_engine, "_run_local_server", prepare)
        result = local_server_engine.run_local_server(
            object(), tmp_path, comfy_url=peer.url, comfy_output_dir="",
            task_identity="task", attempt_identity="attempt", run_inputs=None)
        assert (result["prompt-1"]["outputs"] == peer.outputs if api == "runtime" else result == {"prompt_id": "prompt-1"})
        assert len(refused) == 1 and len(preparations) == 2
        assert preparations[0].generation.identity != preparations[1].generation.identity
        assert preparations[1].generation.sequence == preparations[0].generation.sequence + 1
        assert peer.counts()["GET /object_info"] == 2
        assert peer.counts()["GET /ws"] == 2
        assert peer.counts()["POST /prompt"] == 1


@pytest.mark.parametrize("api", ["runtime", "direct"])
def test_retirement_after_post_never_claims_prompt_not_started(api):
    with ComfyPeer() as peer, ComfyTargetSession(ComfyTarget(peer.url, "attached")) as owner:
        client = owner.client()
        runtime = TargetRuntimeClient(client)
        if api == "runtime":
            asyncio.run(runtime._post_prompt({}))
        else:
            client.submit_prompt({"prompt": {}})
        drop_before_post(owner, peer)
        for operation in (client.system_stats, lambda: client.submit_prompt({"prompt": {}})):
            with pytest.raises(TransportError) as caught:
                operation()
            assert not getattr(caught.value, "prompt_not_started", False)
        assert client._prompt_attempted and client.owns_prompt("prompt-1")
        asyncio.run(runtime.release_attempt())
        assert peer.counts()["POST /prompt"] == 1
