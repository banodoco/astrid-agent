"""RunPod machine observations, based on recorded provider responses."""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

from astrid.core.generation.engine_contracts import PlacementRefused
from astrid.core.generation.machine_provider import MachineRecord

# Top-level keys recorded for each projection, not the provider's published schema.
_REST_KEYS = frozenset("""consumerUserId containerDiskInGb costPerHr createdAt desiredStatus env
    gpuCount id imageName lastStartedAt lastStatusChange machine machineId memoryInGb name
    networkVolume networkVolumeId ports publicIp templateId vcpuCount volumeInGb volumeMountPath""".split())
_SDK_KEYS = frozenset("""containerDiskInGb costPerHr desiredStatus dockerArgs dockerId env gpuCount id
    imageName lastStatusChange machine machineId memoryInGb name podType port ports runtime
    uptimeSeconds vcpuCount volumeInGb volumeMountPath""".split())


# Which record keys evidence each MachineRecord field; absent keys mean "not observed".
_OBSERVES = {
    "name": ("name",), "image": ("imageName", "image"),
    "storage_id": ("networkVolumeId", "networkVolume"), "storage_mount": ("volumeMountPath",),
    "gpu_count": ("gpuCount",), "gpu_label": ("machine", "gpu"),
    "region": ("dataCenterId", "networkVolume"), "hourly_usd": ("costPerHr",),
}


def _parse(record: Mapping[str, Any], keys: frozenset[str], *, strict: bool, sdk: bool) -> MachineRecord:
    if strict:
        unknown = record.keys() - keys
        if unknown:
            raise ValueError(f"unknown pod keys: {sorted(unknown)}")
        for key, allowed in (("env", {"ASTRID_POD_AGENT_B64", "ASTRID_POD_PORT", "ASTRID_POD_TOKEN", "PUBLIC_KEY"}),
                             ("machine", {"gpuDisplayName"} if sdk else set()),
                             ("networkVolume", {"id", "name", "size", "dataCenterId"}),
                             ("runtime", {"ports"})):
            value = record.get(key)
            if isinstance(value, Mapping) and value.keys() - allowed:
                raise ValueError(f"unknown {key} keys: {sorted(value.keys() - allowed)}")
        if sdk and isinstance(record.get("env"), list):
            for entry in record["env"]:
                if isinstance(entry, Mapping) and entry.keys() - {"key", "value"}:
                    raise ValueError("unknown environment entry keys")
    volume_id = record.get("networkVolumeId")
    volume = record.get("networkVolume")
    if volume is not None:
        if not isinstance(volume, Mapping) or not isinstance(volume.get("id"), str) or not volume["id"].strip():
            raise ValueError("invalid network volume")
        nested_id = volume["id"].strip()
    else:
        nested_id = None
    if volume_id is not None:
        if not isinstance(volume_id, str) or not volume_id.strip():
            raise ValueError("invalid network volume id")
        volume_id = volume_id.strip()
    if "networkVolumeId" in record and "networkVolume" in record and volume_id != nested_id:
        raise ValueError("conflicting network volume ids")
    machine = record.get("machine") or {}
    gpu = record.get("gpu") or {}
    state = {"RUNNING": "running", "EXITED": "gone", "TERMINATED": "gone",
             "STOPPED": "stopped"}.get(record.get("desiredStatus"), "starting")
    observed = {"state"} | {name for name, keys in _OBSERVES.items() if any(key in record for key in keys)}
    return MachineRecord(
        observed=frozenset(observed),
        provider="runpod", machine_id=record.get("id"), state=state,
        name=record.get("name"), image=record.get("imageName"),
        storage_id=volume_id or nested_id, storage_mount=record.get("volumeMountPath"),
        gpu_count=record.get("gpuCount"),
        gpu_label=(machine.get("gpuDisplayName") if isinstance(machine, Mapping) else None)
        or (gpu.get("displayName") if isinstance(gpu, Mapping) else None),
        region=(machine.get("dataCenterId") if isinstance(machine, Mapping) else None)
        or (volume.get("dataCenterId") if isinstance(volume, Mapping) else None)
        or record.get("dataCenterId"), hourly_usd=record.get("costPerHr"),
    )


def parse_sdk_pod(record: Mapping[str, Any], *, strict: bool = False) -> MachineRecord:
    return _parse(record, _SDK_KEYS, strict=strict, sdk=True)


def parse_rest_pod(record: Mapping[str, Any], *, strict: bool = False) -> MachineRecord:
    return _parse(record, _REST_KEYS, strict=strict, sdk=False)


def parse_rest_list_item(record: Mapping[str, Any], *, strict: bool = False) -> MachineRecord:
    return _parse(record, _REST_KEYS, strict=strict, sdk=False)


class ProviderRefused(PlacementRefused):
    """The account cannot admit another observation within this budget."""

    def __init__(self, retry_after: float):
        super().__init__("provider account is cooling down", gate="rate",
                         retryable=True, retry_after=retry_after)


def provider_failure(exc):
    """Extract status and delay without interpreting arbitrary exception text."""
    import re

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", getattr(exc, "status_code", None))
    retry_after = getattr(exc, "retry_after", None)
    if response is not None:
        retry_after = getattr(response, "headers", {}).get("Retry-After", retry_after)
    # TODO: lifecycle must expose typed status and Retry-After (currently lost).
    if isinstance(exc, RuntimeError):
        match = re.match(r"^RunPod pod (?:listing|termination) failed: HTTP (\d{3}):", str(exc))
        if match:
            status = int(match.group(1))
    # Lifecycle wraps definite capacity/request responses in LaunchFailure.
    # The typed cause retains the status; a definite 2xx stock-out must not
    # become a transport failure and throttle a journaled capacity retry.
    cause = getattr(exc, "__cause__", None)
    if status is None and cause is not None:
        response = getattr(cause, "response", None)
        status = getattr(response, "status_code", getattr(cause, "status_code", None))
        retry_after = getattr(cause, "retry_after", retry_after)
        if response is not None:
            retry_after = getattr(response, "headers", {}).get("Retry-After", retry_after)
        from runpod_lifecycle.errors import ProviderCapacityUnavailable

        if status != 429 and isinstance(cause, ProviderCapacityUnavailable):
            # An explicit capacity rejection is stock evidence, including
            # lifecycle's 503 capacity response, rather than account failure.
            status = 200
    return status, retry_after


class ProviderNotStarted(ProviderRefused):
    """Admission failed before the provider operation was invoked."""


class ProviderGate:
    """Cross-process account pacing; timestamps are shared wall-clock seconds."""

    def __init__(self, path, *, clock=None, sleep=None, minimum_interval=0.05, alias_path=None):
        import time
        from pathlib import Path

        self.path = Path(path)
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.minimum_interval = minimum_interval
        self.alias_path = alias_path

    # Unknown pacing state gets one persisted recovery window. Valid provider
    # Retry-After values are never shortened to this recovery bound.
    RECOVERY_COOLDOWN_SECONDS = 60.0

    @staticmethod
    def _validate_state(state):
        import math
        import re

        if not isinstance(state, dict) or state.keys() - {"next", "cooldown", "attempt", "account"}:
            raise ValueError("invalid provider gate state")
        for key in ("next", "cooldown"):
            value = state.get(key, 0.)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid provider gate timestamp")
        attempt = state.get("attempt", 0)
        if type(attempt) is not int or attempt < 0:
            raise ValueError("invalid provider gate attempt")
        if "account" in state and (not isinstance(state["account"], str)
                                   or re.fullmatch(r"[0-9a-f]{64}", state["account"]) is None):
            raise ValueError("invalid provider account alias")
        return state

    def _replace_state(self, state):
        import json
        import os
        import tempfile

        self._validate_state(state)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _state(self, change, *, deadline=None):
        import json
        import logging
        import os

        from runtime_protocol import platform

        if self.alias_path is not None:
            # Keep the alias locked through account state mutation. Binding
            # takes these locks in the same order, so an operation admitted on
            # an old credential cannot later publish into an abandoned gate.
            alias = ProviderGate(self.alias_path, clock=self.clock, sleep=self.sleep,
                                 minimum_interval=self.minimum_interval)

            def routed(binding):
                account = binding.get("account", self.path.stem)
                target = ProviderGate(self.path.parent / (account + ".json"), clock=self.clock,
                                      sleep=self.sleep, minimum_interval=self.minimum_interval)

                def apply(state):
                    for key in ("next", "cooldown"):
                        state[key] = max(state.get(key, 0.), binding.get(key, 0.))
                    return change(state)

                return target._state(apply, deadline=deadline)

            return alias._state(routed, deadline=deadline)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(lock_path, os.O_RDWR)
        # This inode is never replaced. Every reader and writer locks it,
        # including alias resolution and recovery, before opening state.
        with os.fdopen(fd, "r+") as lock:
            if deadline is None:
                deadline = _provider_deadline.get()
            limit = self.clock() + 30. if deadline is None else deadline
            while True:
                _check_provider_cancelled()
                try:
                    platform.lock_file(lock.fileno(), blocking=False)
                    break
                except BlockingIOError:
                    remaining = limit - self.clock()
                    if remaining <= 0:
                        raise ProviderRefused(self.minimum_interval) from None
                    self.sleep(min(0.05, remaining))
            initialized = bool(lock.read(1))
            recovered = False
            candidate = None
            try:
                candidate = json.loads(self.path.read_text(encoding="utf-8"))
                state = self._validate_state(candidate)
            except (OSError, UnicodeError, ValueError):
                if not initialized and not self.path.exists():
                    state = {}
                else:
                    state = {"cooldown": self.clock() + self.RECOVERY_COOLDOWN_SECONDS, "attempt": 1}
                    # A bad field must not erase independently valid evidence
                    # of a longer cooldown or a verified account alias.
                    if isinstance(candidate, dict):
                        for key in ("next", "cooldown", "attempt", "account"):
                            if key not in candidate:
                                continue
                            try:
                                self._validate_state({key: candidate[key]})
                            except ValueError:
                                continue
                            state[key] = (candidate[key] if key == "account" else
                                          max(state.get(key, 0), candidate[key]))
                    logging.getLogger(__name__).warning(
                        "Recovering unreadable provider pacing state; conservative cooldown %.0fs",
                        self.RECOVERY_COOLDOWN_SECONDS,
                    )
                recovered = True
            previous = dict(state)
            result = change(state)
            if recovered or state != previous:
                self._replace_state(state)
            if not initialized:
                # Publish this only after the initial state is durable. Any
                # contender can finish initialization under the same lock.
                lock.seek(0)
                lock.write("1")
                lock.flush()
                os.fsync(lock.fileno())
            return result

    def before_call(self, deadline):
        while True:
            _check_provider_cancelled()
            def admit(state):
                now = self.clock()
                delay = max(0., state.get("next", 0.) - now, state.get("cooldown", 0.) - now)
                if delay <= 0:
                    state["next"] = now + self.minimum_interval
                return delay
            delay = self._state(admit, deadline=deadline)
            if self.clock() > deadline:
                raise ProviderRefused(max(0., delay))
            if delay <= 0:
                return
            if self.clock() + delay > deadline:
                raise ProviderRefused(delay)
            self.sleep(min(60., delay))

    def after_response(self, status, retry_after=None):
        if status == 429 or status is not None and status >= 500:
            self.after_error(retry_after)
        elif status is not None and status < 400 or status == 404:
            # Success resets the exponent, never another caller's cooldown.
            self._state(lambda state: state.update(attempt=0))

    def after_error(self, retry_after=None):
        from email.utils import parsedate_to_datetime

        from astrid.core.generation.engine_contracts import next_delay

        try:
            delay = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            try:
                delay = max(0., parsedate_to_datetime(retry_after).timestamp() - self.clock())
            except (TypeError, ValueError, OverflowError):
                delay = None
        def update(state):
            attempt = state.get("attempt", 0) + 1
            state.update(attempt=attempt, cooldown=max(state.get("cooldown", 0.),
                         self.clock() + next_delay(attempt, delay)))
        self._state(update)

    def retry_delay(self, *, minimum_seconds=5.0):
        return self._state(lambda state: min(60., max(minimum_seconds, state.get("cooldown", 0.) - self.clock(),
                                                      state.get("next", 0.) - self.clock())))

    def call(self, operation, *args, deadline=None, **kwargs):
        cleanup = _provider_cleanup.get()
        deadline = self.clock() + 30. if deadline is None else deadline
        if not cleanup:
            try:
                self.before_call(deadline)
            except ProviderRefused as exc:
                raise ProviderNotStarted(exc.retry_after) from exc
        _check_provider_cancelled()
        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            status, retry_after = provider_failure(exc)
            self._account_response(status, retry_after, cleanup=cleanup, deadline=deadline)
            raise
        self._account_response(getattr(result, "status_code", 200),
                               getattr(result, "headers", {}).get("Retry-After"), cleanup=cleanup, deadline=deadline)
        return result

    def _account_response(self, status, retry_after=None, *, cleanup=False, deadline=None):
        import logging

        if cleanup:
            return
        cancel_token = _provider_cancelled.set(None)
        deadline_token = _provider_deadline.set(deadline if deadline is not None else _provider_deadline.get())
        try:
            if status is None:
                self.after_error(retry_after)
            else:
                self.after_response(status, retry_after)
        except (OSError, ProviderRefused):
            # Auxiliary bookkeeping must not change a known create response
            # into an apparent create failure. The journal owns that response.
            logging.getLogger(__name__).warning("Provider pacing response could not be persisted")
        finally:
            _provider_deadline.reset(deadline_token)
            _provider_cancelled.reset(cancel_token)


def account_gate(api_key, *, account_ref=None):
    import hashlib

    from astrid.sdk.storage_root import resolve_runtime_data_root

    root = resolve_runtime_data_root() / "runpod" / "provider-gates"
    credential = hashlib.sha256(api_key.encode()).hexdigest()
    if _provider_cleanup.get():
        # Exact cleanup must reach the provider even if aliases/state cannot be
        # read, repaired, locked or persisted. It never authorizes a create.
        return ProviderGate(root / (credential + ".json"))
    alias = ProviderGate(root / (credential + ".account.json"))

    def resolve(state):
        current = state.get("account", credential)
        if account_ref is not None:
            account = hashlib.sha256(("account:" + account_ref).encode()).hexdigest()
            if current != account:
                # Preserve a cooldown observed before the journal supplied its
                # stable account identity. Register before starting operations.
                previous = ProviderGate(root / (current + ".json"))._state(dict)
                def merge(shared):
                    for key in ("next", "cooldown", "attempt"):
                        shared[key] = max(shared.get(key, 0), previous.get(key, 0), state.get(key, 0))
                ProviderGate(root / (account + ".json"))._state(merge)
                state["account"] = account
            return account
        return current

    account = alias._state(resolve)
    return ProviderGate(root / (account + ".json"), alias_path=alias.path)


_provider_deadline = ContextVar("provider_deadline", default=None)
_provider_cleanup = ContextVar("provider_cleanup", default=False)
_provider_cancelled = ContextVar("provider_cancelled", default=None)


def _check_provider_cancelled():
    cancelled = _provider_cancelled.get()
    if cancelled is not None:
        cancelled()



@contextmanager
def provider_budget(seconds, *, cleanup=False, cancelled=None):
    import time

    existing = (_provider_deadline.get() if not cleanup or _provider_cleanup.get() else None)
    deadline = time.time() + seconds
    token = _provider_deadline.set(min(existing, deadline) if existing is not None else deadline)
    cleanup_token = _provider_cleanup.set(cleanup or _provider_cleanup.get())
    cancel_token = _provider_cancelled.set(None if cleanup else cancelled or _provider_cancelled.get())
    try:
        yield
    finally:
        _provider_cancelled.reset(cancel_token)
        _provider_cleanup.reset(cleanup_token)
        _provider_deadline.reset(token)


def provider_remaining_budget():
    import time

    deadline = _provider_deadline.get()
    return 30. if deadline is None else max(0., min(30., deadline - time.time()))


def provider_call(api_key, operation, *args, **kwargs):
    return account_gate(api_key).call(operation, *args, deadline=_provider_deadline.get(), **kwargs)


async def async_provider_call(api_key, operation, *args, already_gated=False, on_late_result=None, **kwargs):
    """Bound the waiter and cancel admission, retaining already-started custody."""
    import asyncio
    from concurrent.futures import Future
    from contextvars import copy_context
    from threading import Event, Thread

    future = Future()
    context = copy_context()
    stopped = Event()
    gate = None

    def execute():
        nonlocal gate
        parent_check = _provider_cancelled.get()

        def check():
            if stopped.is_set():
                raise asyncio.CancelledError()
            if parent_check is not None:
                parent_check()

        _provider_cancelled.set(check)
        check()
        if already_gated:
            return operation(*args, **kwargs)
        try:
            gate = account_gate(api_key)
        except ProviderRefused as exc:
            raise ProviderNotStarted(exc.retry_after) from exc
        return gate.call(operation, *args, deadline=_provider_deadline.get(), **kwargs)

    def run():
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = context.run(execute)
        except BaseException as exc:  # noqa: BLE001 - forward worker cancellation/failures.
            future.set_exception(exc)
        else:
            future.set_result(result)

    Thread(target=run, daemon=True, name="provider-call").start()

    def retain_late_result():
        if on_late_result is None:
            return

        def completed(done):
            try:
                result = done.result()
            except BaseException:  # noqa: BLE001 - the pending journal owns uncertain failures
                return  # The pending journal already owns uncertain failures.

            def persist():
                import logging

                try:
                    on_late_result(result)
                except BaseException:  # noqa: BLE001 - late-result persistence must not raise from the callback
                    logging.getLogger(__name__).error(
                        "Late provider result could not be persisted; allocation requires reconciliation")

            # The callback may need the journal lock still held by the caller
            # while cancellation unwinds. Never wait for it on the event loop.
            Thread(target=persist, daemon=True, name="provider-late-result").start()

        # Also handles a result arriving between waiter cancellation and here.
        future.add_done_callback(completed)

    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=provider_remaining_budget())
    except TimeoutError:
        stopped.set()
        retain_late_result()
        if gate is not None and not context.get(_provider_cleanup):
            # Never resolve an alias or wait for a lock on the event loop after
            # timeout. Use the already resolved gate and an immediate lock budget.
            recorded = Future()

            def record_timeout():
                try:
                    _provider_deadline.set(gate.clock())
                    gate._account_response(None)
                finally:
                    recorded.set_result(None)

            Thread(target=record_timeout, daemon=True, name="provider-timeout").start()
            await asyncio.wrap_future(recorded)
        raise
    except asyncio.CancelledError:
        stopped.set()
        retain_late_result()
        raise


async def async_provider_operation(api_key, operation, *args, on_late_result=None, **kwargs):
    """One admitted lifecycle coroutine; never retry an ambiguous create here."""
    import asyncio

    return await async_provider_call(api_key, lambda: asyncio.run(operation(*args, **kwargs)),
                                     on_late_result=on_late_result)


class RunPodMachineProvider:
    kind = "runpod"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def read_raw(self, machine_id: str) -> dict[str, Any] | None:
        import httpx
        from runpod_lifecycle.api import PODS_URL

        response = provider_call(self.api_key, httpx.get,
            f"{PODS_URL}/{quote(machine_id, safe='')}",
            headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30,
            params={"includeNetworkVolume": "true"},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ValueError("exact pod GET did not succeed")
        record = response.json()
        if not isinstance(record, dict) or record.get("id") != machine_id:
            raise ValueError("exact pod GET returned an unverified identity")
        return record

    def read(self, machine_id: str) -> MachineRecord | None:
        record = self.read_raw(machine_id)
        return parse_rest_pod(record) if record is not None else None

    def find(self, request_name: str) -> list[MachineRecord]:
        import httpx
        from runpod_lifecycle.api import PODS_URL

        response = provider_call(self.api_key, httpx.get,PODS_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                             timeout=30, params={"includeNetworkVolume": "true"})
        response.raise_for_status()
        records = response.json()
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError("invalid pod list")
        return [parse_rest_list_item(item) for item in records if item.get("name") == request_name]
