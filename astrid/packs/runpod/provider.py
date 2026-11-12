"""RunPod machine observations, based on recorded provider responses."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

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


class RunPodMachineProvider:
    kind = "runpod"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def read_raw(self, machine_id: str) -> dict[str, Any] | None:
        import httpx
        from runpod_lifecycle.api import PODS_URL

        response = httpx.get(
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

        response = httpx.get(PODS_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                             timeout=30, params={"includeNetworkVolume": "true"})
        response.raise_for_status()
        records = response.json()
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError("invalid pod list")
        return [parse_rest_list_item(item) for item in records if item.get("name") == request_name]
