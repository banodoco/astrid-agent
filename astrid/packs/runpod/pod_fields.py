"""Read identity evidence from SDK and REST pod projections."""

from collections.abc import Mapping
from typing import Any

from runpod_lifecycle.rest import normalize_pod


class InvalidPodField(ValueError):
    """A provider projection contains malformed or contradictory evidence."""


def pod_field(pod: Mapping[str, Any], field: str) -> Any:
    """Read a canonical field, rejecting malformed or conflicting aliases."""
    try:
        pod = normalize_pod(dict(pod))
    except ValueError:
        raise InvalidPodField(field) from None
    if field == "volume" and len((pod.get("mounts") or {}).get("network", [])) > 1:
        raise InvalidPodField(field)
    aliases = {"image": ("imageName", "image"), "volume": ("networkVolumeId", "networkVolume"),
               "gpu_count": ("gpuCount", "gpu"), "gpu_name": ("machineType", "gpu")}
    keys = aliases.get(field, (field,))
    values = []
    for key in keys:
        if key not in pod:
            continue
        value = pod[key]
        if key == "networkVolume" and value is not None:
            if not isinstance(value, Mapping) or not isinstance(value.get("id"), str) or not value["id"].strip():
                raise InvalidPodField(field)
            value = value["id"]
        if key == "gpu" and value is not None:
            if not isinstance(value, Mapping):
                raise InvalidPodField(field)
            value = value.get("count") if field == "gpu_count" else value.get("id", value.get("displayName"))
        if field == "gpu_count":
            if value is not None and (type(value) is not int or value < 0):
                raise InvalidPodField(field)
            values.append(value)
            continue
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InvalidPodField(field)
        values.append(value.strip() if isinstance(value, str) else None)
    if len(values) > 1 and any(value != values[0] for value in values[1:]):
        raise InvalidPodField(field)
    return values[0] if values else None
