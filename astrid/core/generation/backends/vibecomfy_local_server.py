"""Attach-only helpers for a user's already-running, loopback ComfyUI."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from astrid.core.generation.engine_contracts import TransportError

from ..vibecomfy_runtime_root import vibecomfy_runtime_root
from .comfy_target import (
    ComfyTarget,
    ComfyTargetClient,
    validate_identity,
)
from .comfy_target import (
    validate_attached_url as validate_comfy_url,
)

__all__ = [
    "cleanup_prompts", "upload_image", "upload_media", "validate_comfy_url",
    "validate_identity", "validate_output_directory",
    "TargetRuntimeClient", "AttachedArtifacts",
]


class TargetRuntimeClient:
    """Adapt the selected G22 client to VibeComfy's existing async runner."""

    def __init__(self, client: ComfyTargetClient, *, on_event=None) -> None:
        self.client = client
        self.server_url = client.http_origin
        self.session = client.held_session
        self._attempt = None
        self._progress = None
        self._on_event = on_event
        self._recovery = None
        self._released = False
        self.evidence = dict(getattr(client, "transport_evidence", {}))
        if self.session is not None:
            self.evidence.update(mode="held", target_generation=client.generation.identity,
                                 target_identity="unverified")

    async def _post_prompt(self, prompt: dict[str, Any]) -> Any:
        if self._released:
            raise TransportError("Comfy attempt is released")
        if self.session is None:
            return await asyncio.to_thread(self.client.submit_prompt, {"prompt": prompt})
        body = self.client._begin_prompt({"prompt": prompt})

        async def post():
            self._attempt = await self.session.attempt(self.client)
            result = await self._attempt._post_prompt(body["prompt"])
            if not isinstance(result, dict):
                raise ValueError("Comfy prompt acceptance is not an object")
            # An acknowledgement that races retirement is still a witness for
            # this old attempt; it is never a licence to post again.
            self.client.witness_prompt(result.get("prompt_id"))
            if self._on_event is not None:
                self._progress = asyncio.create_task(self._consume_events())
            return result

        try:
            return await self.session.wait(post())
        except HTTPError as exc:
            if exc.code in {400, 401, 403, 404, 422}:
                raise
            raise self.client._unknown_acceptance() from exc
        except Exception as exc:
            raise self.client._unknown_acceptance() from exc

    async def _consume_events(self):
        while True:
            event = await self._attempt.next_event()
            if event is None:
                return
            data = event.get("data", {})
            if not self.client.owns_prompt(data.get("prompt_id")):
                continue
            try:
                # VibeComfy coalesces into one slot. Slow display work cannot
                # block its socket reader, history-ready event or worker exit.
                await self._publish_event(event)
            except Exception:  # noqa: BLE001 - an advisory consumer cannot fail terminal custody
                pass  # Progress is advisory; terminal custody is not.

    async def _publish_event(self, event):
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        context = contextvars.copy_context()

        def finish():
            if not finished.done():
                finished.set_result(None)

        def publish():
            try:
                if not self._released:
                    context.run(self._on_event, event)
            except Exception:  # noqa: BLE001 - consumer errors are advisory
                pass
            finally:
                try:
                    loop.call_soon_threadsafe(finish)
                except RuntimeError:
                    pass  # The owning job already closed its event loop.

        # ThreadPoolExecutor joins its workers at interpreter exit, even after
        # cancelling the awaiting task. One active daemon callback per attempt avoids
        # making a stalled display consumer part of the job's terminal path.
        # Awaiting this callback leaves subsequent events in Vibe's single slot.
        threading.Thread(target=publish, name="comfy-progress", daemon=True).start()
        await finished

    async def history_ready(self, prompt_id: str) -> bool:
        if not self.client.owns_prompt(prompt_id):
            raise ValueError("runtime history requires this attempt's witnessed prompt")
        if self.session is None:
            return False
        ready = await self.session.wait(self._attempt.history_ready(prompt_id))
        if not ready or self.client.generation.retired:
            self._recovery_client(prompt_id)
            return False
        return True

    def _recovery_client(self, prompt_id):
        if not self.client.owns_prompt(prompt_id):
            raise ValueError("reconciliation requires this attempt's witnessed prompt")
        if self._recovery is None:
            # No shared witness set, submission capability, target facts or
            # upload cache is transferred to this bounded recovery connection.
            self._recovery = ComfyTargetClient(self.client.target)
            self._recovery._prompt_attempted = True
            self._recovery.witness_prompt(prompt_id)
            import uuid
            self.evidence.update(mode="bounded-poll-disconnect", recovery_generation=uuid.uuid4().hex,
                                 reason=self.client.generation.reason)
        if not self._recovery.owns_prompt(prompt_id):
            raise ValueError("reconciliation belongs to another prompt")
        return self._recovery

    def artifact_client(self, prompt_id):
        if self.session is not None and self.client.generation.retired:
            return self._recovery_client(prompt_id)
        return self.client

    async def history(self, prompt_id: str) -> Any:
        if not self.client.owns_prompt(prompt_id):
            raise ValueError("runtime history requires this attempt's witnessed prompt")
        if self.session is None or self._attempt is None:
            return await asyncio.to_thread(self.client.history, prompt_id)
        if self.client.generation.retired:
            return await asyncio.to_thread(self._recovery_client(prompt_id).history, prompt_id)
        try:
            result = await self.session.wait(self._attempt.history(prompt_id))
            return self.client._current(result)
        except (TransportError, OSError, ValueError):
            if not self.client.generation.retired:
                raise
            return await asyncio.to_thread(self._recovery_client(prompt_id).history, prompt_id)

    async def release_attempt(self):
        if self._released:
            return
        self._released = True
        if self.session is not None and self._attempt is not None:
            async def release():
                self._attempt.release()
                if self._progress is not None:
                    self._progress.cancel()
                    await asyncio.gather(self._progress, return_exceptions=True)
            await self.session.wait(release())


class AttachedArtifacts:
    """Use the runner's existing attributed-artifact seam with G22 /view custody."""

    def __init__(self, client: ComfyTargetClient, *, legacy_output_directory: str = "", runtime_client=None) -> None:
        if client.target.kind != "attached":
            raise ValueError("attached artifact custody requires an attached target")
        self.client = client
        self.runtime_client = runtime_client
        self.server_url = client.http_origin
        self.legacy_output_directory = Path(legacy_output_directory) if legacy_output_directory else None

    def describe(self) -> dict[str, Any]:
        return {"transport": "comfy-http-view", "target_kind": "attached",
                "endpoint_url": self.client.target.url,
                **({"connection": dict(self.runtime_client.evidence)} if self.runtime_client is not None else {})}

    async def download_artifacts(self, *, local_root: Path) -> dict[str, Any]:
        from .comfy_target import validate_output_descriptor

        root = Path(local_root)
        attempt = json.loads((root.parent / "attempt.json").read_text(encoding="utf-8"))
        prompt_id = attempt.get("prompt_id")
        acceptance = attempt.get("queue_acceptance")
        if (not isinstance(prompt_id, str)
                or not isinstance(acceptance, dict)
                or acceptance.get("status") != "accepted"
                or acceptance.get("prompt_id") != prompt_id
                or not self.client.owns_prompt(prompt_id)):
            raise ValueError("artifact custody requires this attempt's witnessed prompt")
        artifacts = attempt.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("accepted history has no attributed artifacts")
        root.mkdir(parents=True, exist_ok=False)
        seen = set()
        for artifact in artifacts:
            descriptor = artifact.get("descriptor") if isinstance(artifact, dict) else None
            filename, subfolder, kind = validate_output_descriptor(descriptor)
            if self.legacy_output_directory is not None and kind == "output":
                # Preserve the legacy caller's safety constraint, without
                # treating its shared folder as artifact custody or reading
                # any bytes from it. New attached callers need no folder.
                candidate = self.legacy_output_directory / subfolder / filename
                if (any(part.is_symlink() for part in (candidate, *candidate.parents))
                        or not candidate.resolve().is_relative_to(self.legacy_output_directory)):
                    raise ValueError("legacy output descriptor is an unsafe symlink or escaped path")
            relative = Path(kind) / subfolder / filename
            if relative in seen:
                raise ValueError("accepted output descriptors are ambiguous")
            seen.add(relative)
            download_client = (self.runtime_client.artifact_client(prompt_id)
                               if self.runtime_client is not None else self.client)
            try:
                await asyncio.to_thread(download_client.download_output, descriptor, root / relative)
            except (TransportError, ValueError):
                if (self.runtime_client is None or download_client is not self.client
                        or self.client.generation is None or not self.client.generation.retired):
                    raise
                await asyncio.to_thread(self.runtime_client.artifact_client(prompt_id).download_output,
                                        descriptor, root / relative)
        return {"status": "retrieved", "local_root": str(root), **self.describe()}


def validate_output_directory(value: str, attempt_root: Path) -> str:
    if not value:
        return ""
    path = Path(value)
    if (not path.is_absolute() or not path.is_dir()
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise ValueError("comfy_output_dir must be an absolute, existing, non-symlink directory")
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(attempt_root.resolve()):
        raise ValueError("comfy_output_dir must be outside the attempt root")
    return str(resolved)


def _request(url: str, path: str, payload: Any = None) -> Any:
    client = ComfyTargetClient(ComfyTarget(url, "attached"))
    if path == "/prompt" and payload is not None:
        return client.submit_prompt(payload)
    if path.startswith("/history/") and payload is None:
        return client.history(path.removeprefix("/history/"))
    return client.request_json(path, payload, timeout=5)


def upload_image(url: str, source: Path, *, task_id: str) -> str:
    return upload_media(url, source, task_id=task_id, kind="image")


def upload_media(
    url: str, source: Path, *, task_id: str, kind: str = "image",
    expected_sha256: str | None = None,
) -> str:
    """Upload verified media using the shared attach-only HTTP client."""
    return ComfyTargetClient(ComfyTarget(url, "attached")).upload_media(
        source, task_id=task_id, kind=kind, expected_sha256=expected_sha256,
    )


def cleanup_prompts(url: str, destination: Path, *, confirm: bool = False, timeout: float = 5.0) -> None:
    """Cancel only prompts witnessed in this attempt's private VibeComfy journal.

    This is safe to repeat after child termination: it neither clears a queue
    nor frees models. The host repeats it after the child has stopped, covering
    SIGKILL/crashes and acceptance that raced with an earlier cancel request.
    """
    client = ComfyTargetClient(ComfyTarget(url, "attached"))
    runs = vibecomfy_runtime_root(destination) / "out" / "runs"
    for path in runs.glob("*/attempt.json"):
        if (path.is_symlink() or not path.is_file()
                or any(part.is_symlink() for part in path.parents)
                or not (path.resolve().is_relative_to(destination.resolve())
                        or path.resolve().is_relative_to(Path(vibecomfy_runtime_root(destination)).resolve()))):
            raise ValueError("local_server attempt journal escaped attempt custody")
        attempt = json.loads(path.read_text(encoding="utf-8"))
        acceptance = attempt.get("queue_acceptance", {})
        prompt_id = acceptance.get("prompt_id")
        status = acceptance.get("status")
        if confirm and status == "not_attempted":
            terminal = attempt.get("terminal") or {}
            # A prepared journal can survive a signal after POST but before
            # acceptance is persisted. Absence of a prompt id is no drainage proof.
            if terminal.get("phase") not in {"dependencies", "reconciliation", "authorization"}:
                raise ValueError("local_server prepared prompt outcome remains unconfirmed")
        if confirm and (status not in {"not_attempted", "rejected", "accepted"}
                        or status == "accepted" and (not isinstance(prompt_id, str) or not prompt_id)):
            raise ValueError("local_server prompt acceptance remains unconfirmed")
        if status != "accepted" or not isinstance(prompt_id, str) or not prompt_id:
            continue
        client.witness_prompt(prompt_id)
        client.cancel_prompt(prompt_id)
        if confirm:
            deadline = time.monotonic() + timeout
            while True:
                queue = client.request_json("/queue")
                identities: set[str] = set()
                for key in ("queue_running", "queue_pending"):
                    rows = queue.get(key) if isinstance(queue, dict) else None
                    if (not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 2
                                                         or not isinstance(row[1], str) for row in rows)):
                        raise ValueError("local_server prompt drainage has invalid queue identities")
                    identities.update(row[1] for row in rows)
                if prompt_id not in identities:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("local_server owned prompt has not drained")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
