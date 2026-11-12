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
    return status, retry_after


class ProviderGate:
    """Cross-process account pacing; timestamps are shared wall-clock seconds."""

    def __init__(self, path, *, clock=None, sleep=None, minimum_interval=0.05):
        import time
        from pathlib import Path

        self.path = Path(path)
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.minimum_interval = minimum_interval

    def _state(self, change, *, deadline=None):
        import json

        from runtime_protocol import platform

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            limit = self.clock() + 30. if deadline is None else deadline
            while True:
                try:
                    platform.lock_file(stream.fileno(), blocking=False)
                    break
                except BlockingIOError:
                    remaining = limit - self.clock()
                    if remaining <= 0:
                        raise ProviderRefused(self.minimum_interval) from None
                    self.sleep(min(0.05, remaining))
            stream.seek(0)
            raw = stream.read()
            state = json.loads(raw) if raw else {}
            result = change(state)
            stream.seek(0)
            stream.truncate()
            json.dump(state, stream)
            stream.flush()
            return result

    def before_call(self, deadline):
        while True:
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
        self.before_call(self.clock() + 30. if deadline is None else deadline)
        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            status, retry_after = provider_failure(exc)
            if status is not None:
                self.after_response(status, retry_after)
            else:
                self.after_error(retry_after)
            raise
        status = getattr(result, "status_code", 200)
        self.after_response(status, getattr(result, "headers", {}).get("Retry-After"))
        return result


def account_gate(api_key, *, account_ref=None):
    import hashlib

    from astrid.sdk.storage_root import resolve_runtime_data_root

    root = resolve_runtime_data_root() / "runpod" / "provider-gates"
    credential = hashlib.sha256(api_key.encode()).hexdigest()
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
                        shared[key] = max(shared.get(key, 0), previous.get(key, 0))
                ProviderGate(root / (account + ".json"))._state(merge)
                state["account"] = account
            return account
        return current

    account = alias._state(resolve)
    return ProviderGate(root / (account + ".json"))


_provider_deadline = ContextVar("provider_deadline", default=None)


@contextmanager
def provider_budget(seconds):
    import time

    existing = _provider_deadline.get()
    deadline = time.time() + seconds
    token = _provider_deadline.set(min(existing, deadline) if existing is not None else deadline)
    try:
        yield
    finally:
        _provider_deadline.reset(token)


def provider_remaining_budget():
    import time

    deadline = _provider_deadline.get()
    return 30. if deadline is None else max(0., min(30., deadline - time.time()))


def provider_call(api_key, operation, *args, **kwargs):
    return account_gate(api_key).call(operation, *args, deadline=_provider_deadline.get(), **kwargs)


async def async_provider_call(api_key, operation, *args, already_gated=False, **kwargs):
    """Bound the caller even when lifecycle is still finishing its own polling.

    An overdue operation retains exact resource custody; its late failure still
    updates the account gate. It must not hold up event-loop executor shutdown.
    """
    import asyncio
    from concurrent.futures import Future
    from contextvars import copy_context
    from threading import Thread

    future = Future()
    context = copy_context()

    def run():
        if not future.set_running_or_notify_cancel():
            return
        try:
            if already_gated:
                result = context.run(operation, *args, **kwargs)
            else:
                result = context.run(provider_call, api_key, operation, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - forward thread failures to its waiting caller.
            future.set_exception(exc)
        else:
            future.set_result(result)

    Thread(target=run, daemon=True, name="provider-cleanup").start()
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=provider_remaining_budget())
    except TimeoutError:
        account_gate(api_key).after_error()
        raise


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
