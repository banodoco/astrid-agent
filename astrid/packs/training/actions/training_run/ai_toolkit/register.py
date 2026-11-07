"""ai-toolkit LoRA registration helpers."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from astrid.pack_api import write_json_atomic
from astrid.packs.training.shared.interfaces import (
    ArtifactPullResult,
    ComputeHandle,
)


class RegisterRemoteBackend(Protocol):
    def pull_artifacts(self, handle: ComputeHandle, remote_paths: list[str], local_dir: Path, config: dict[str, Any]) -> ArtifactPullResult:
        ...


@dataclass(frozen=True)
class RegistrationResult:
    lora_id: str
    pulled_checkpoint_path: Path
    registered_lora_path: Path
    metadata_path: Path
    pull_result: ArtifactPullResult


def register_checkpoint(
    remote: RegisterRemoteBackend,
    handle: ComputeHandle,
    *,
    checkpoint_remote_path: str,
    local_dir: Path,
    registry_dir: Path,
    lora_id: str,
    metadata: Mapping[str, Any] | None = None,
    produces_dir: Path | None = None,
    ssh_key: Path | None = None,
) -> RegistrationResult:
    """Pull a selected checkpoint, verify it, and register local LoRA artifacts."""
    if not checkpoint_remote_path.endswith(".safetensors"):
        raise ValueError("registered checkpoint must be a .safetensors artifact")
    local_dir = Path(local_dir).expanduser().resolve()
    registry_dir = Path(registry_dir).expanduser().resolve()
    local_dir.mkdir(parents=True, exist_ok=True)
    registry_dir.mkdir(parents=True, exist_ok=True)

    pull_config: dict[str, Any] = {}
    if produces_dir is not None:
        pull_config["produces_dir"] = Path(produces_dir)
    if ssh_key is not None:
        pull_config["ssh_key"] = Path(ssh_key)
    pull_result = remote.pull_artifacts(handle, [checkpoint_remote_path], local_dir, pull_config)
    pulled = _resolve_pulled_checkpoint(checkpoint_remote_path, pull_result.local_paths, local_dir)
    registered = registry_dir / pulled.name
    shutil.copy2(pulled, registered)

    metadata_path = registry_dir / "registered_lora.json"
    payload = {
        "schema_version": 1,
        "lora_id": lora_id,
        "source_checkpoint_remote_path": checkpoint_remote_path,
        "pulled_checkpoint_path": str(pulled),
        "registered_lora_path": str(registered),
        "metadata": dict(metadata or {}),
    }
    write_json_atomic(metadata_path, payload)
    return RegistrationResult(lora_id=lora_id, pulled_checkpoint_path=pulled, registered_lora_path=registered, metadata_path=metadata_path, pull_result=pull_result)


def _resolve_pulled_checkpoint(remote_path: str, local_paths: list[Path], local_dir: Path) -> Path:
    candidates = [Path(path) for path in local_paths if Path(path).suffix == ".safetensors"]
    candidates.append(local_dir / Path(remote_path).name)
    for candidate in candidates:
        if candidate.exists() and candidate.suffix == ".safetensors":
            return candidate
    raise FileNotFoundError(f"pulled checkpoint is missing: {local_dir / Path(remote_path).name}")
