"""RunPod storage helpers — ensure-storage and volume listing."""

from __future__ import annotations

import asyncio
from typing import Any

from astrid.core.util.credentials_scope import CredentialsScope

ENSURE_STORAGE_HINT = (
    "Run `python3 -m astrid runpod ensure-storage <storage-name> --size <GB> "
    "--datacenter <id>` first, then pass `--storage-name <storage-name>`."
)


async def ensure_storage(
    name: str,
    *,
    size_gb: int = 50,
    datacenter_id: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Find or create a RunPod network volume by *name*.

    Calls the lifecycle API with the resolved credential; if missing, creates
    the volume only when a datacenter is explicitly supplied.

    Idempotent — no cost event emitted.

    Parameters
    ----------
    name:
        Volume name to find or create.
    size_gb:
        Size in GB for the new volume (only used on create).  Default 50.
    datacenter_id:
        RunPod datacenter ID (e.g. ``"US-GA-1"``).  Required when creating
        a new volume; raises :class:`ValueError` if omitted and the volume
        does not exist.
    api_key:
        Optional explicit RunPod API key. When omitted, resolves the
        ``RUNPOD_API_KEY`` reference through Astrid's canonical credential
        resolver.
    """
    from runpod_lifecycle import api

    resolved_key = api_key or CredentialsScope.get_local("runpod")
    volumes = await asyncio.to_thread(api.get_network_volumes, resolved_key)
    existing = next(
        (
            volume
            for volume in volumes
            if volume.get("id") == name or volume.get("name") == name
        ),
        None,
    )
    if existing is not None:
        return existing

    if datacenter_id is None:
        raise ValueError(
            f"datacenter_id is required to create a new network volume "
            f"(volume {name!r} not found)"
        )

    return await asyncio.to_thread(
        api.create_network_volume,
        resolved_key,
        name,
        size_gb,
        datacenter_id,
    )


async def require_existing_storage(
    name: str | None,
    *,
    context: str = "RunPod storage",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Return an existing RunPod network volume, or fail without creating one."""
    if not name:
        raise ValueError(f"{context} requires a pre-existing RunPod network storage volume. {ENSURE_STORAGE_HINT}")

    from runpod_lifecycle import api

    resolved_key = api_key or CredentialsScope.get_local("runpod")
    volumes = await asyncio.to_thread(api.get_network_volumes, resolved_key)
    existing = next(
        (
            volume
            for volume in volumes
            if volume.get("id") == name or volume.get("name") == name
        ),
        None,
    )
    if existing is None:
        raise ValueError(f"RunPod network storage volume {name!r} was not found. {ENSURE_STORAGE_HINT}")
    return existing


async def list_volumes(api_key: str | None = None) -> list[dict[str, Any]]:
    """Return all RunPod network volumes for the account.

    Thin passthrough to :func:`api.get_network_volumes`.

    Parameters
    ----------
    api_key:
        Optional explicit RunPod API key. When omitted, resolves the
        ``RUNPOD_API_KEY`` reference through Astrid's canonical credential
        resolver.
    """
    from runpod_lifecycle import api

    resolved_key = api_key or CredentialsScope.get_local("runpod")

    return await asyncio.to_thread(api.get_network_volumes, resolved_key)
