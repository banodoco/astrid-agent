"""Registry for generation backend adapters.

The registry keeps backend descriptors inert until a caller asks to
instantiate one.  This lets pack manifests register third-party backend
module/class pairs without importing them during discovery or listing.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.generation.backends.base import BackendAdapter
from astrid.core.model_catalog.taxonomy import (
    CLOUD_BACKEND_ID,
    CODEX_BACKEND_ID,
    LOCAL_BACKEND_ID,
    WAVESPEED_BACKEND_ID,
)
from astrid.core.pack import PackDefinition, discover_packs
from astrid.core.pack.discovery import discover_pack_metadata


@dataclass(frozen=True)
class GenerationBackendDescriptor:
    """Descriptor for one generation backend adapter class."""

    backend_id: str
    module: str
    class_name: str
    label: str = ""
    init_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def create(
        self,
        *,
        env_file: Path | None = None,
        init_overrides: Mapping[str, Any] | None = None,
    ) -> BackendAdapter:
        """Instantiate the described backend lazily."""
        backend_cls = self.load_class()
        kwargs = dict(self.init_kwargs)
        if init_overrides:
            kwargs.update(init_overrides)
        if env_file is not None and _accepts_keyword(backend_cls, "env_file"):
            kwargs.setdefault("env_file", env_file)
        instance = backend_cls(**kwargs)
        if not isinstance(instance, BackendAdapter):
            raise TypeError(
                f"generation backend {self.backend_id!r} must create a "
                f"BackendAdapter, got {type(instance).__name__}"
            )
        return instance

    def load_class(self) -> type[BackendAdapter]:
        """Import and return the backend adapter class."""
        module = importlib.import_module(self.module)
        backend_cls = getattr(module, self.class_name)
        if not isinstance(backend_cls, type):
            raise TypeError(
                f"generation backend {self.backend_id!r} resolved "
                f"{self.module}.{self.class_name} to a non-class object"
            )
        return backend_cls


class GenerationBackendRegistry:
    """Registry of built-in and pack-provided generation backends."""

    def __init__(
        self,
        descriptors: list[GenerationBackendDescriptor] | tuple[GenerationBackendDescriptor, ...] | None = None,
    ) -> None:
        self._descriptors: dict[str, GenerationBackendDescriptor] = {}
        self.register_many(_builtin_generation_backend_descriptors())
        self.register_many(descriptors or ())

    def register(self, descriptor: GenerationBackendDescriptor) -> None:
        existing = self._descriptors.get(descriptor.backend_id)
        if existing is not None:
            raise ValueError(
                f"duplicate generation backend id {descriptor.backend_id!r}: "
                f"{existing.module}.{existing.class_name} and "
                f"{descriptor.module}.{descriptor.class_name}"
            )
        self._descriptors[descriptor.backend_id] = descriptor

    def register_many(
        self,
        descriptors: list[GenerationBackendDescriptor] | tuple[GenerationBackendDescriptor, ...],
    ) -> None:
        for descriptor in descriptors:
            self.register(descriptor)

    def descriptors(self) -> tuple[GenerationBackendDescriptor, ...]:
        return tuple(
            self._descriptors[backend_id]
            for backend_id in sorted(self._descriptors)
        )

    def get_descriptor(self, backend_id: str) -> GenerationBackendDescriptor:
        try:
            return self._descriptors[backend_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._descriptors)) or "(none)"
            raise KeyError(
                f"unknown generation backend {backend_id!r}; available: {available}"
            ) from exc

    def create(
        self,
        backend_id: str,
        *,
        env_file: Path | None = None,
        init_overrides: Mapping[str, Any] | None = None,
    ) -> BackendAdapter:
        return self.get_descriptor(backend_id).create(
            env_file=env_file,
            init_overrides=init_overrides,
        )


def descriptors_from_pack(pack: PackDefinition) -> tuple[GenerationBackendDescriptor, ...]:
    """Return inert backend descriptors declared by one pack manifest."""
    generation = pack.extensions.get("generation")
    if not isinstance(generation, dict):
        return ()
    backends = generation.get("backends")
    if not isinstance(backends, list):
        return ()

    descriptors: list[GenerationBackendDescriptor] = []
    for raw_backend in backends:
        if not isinstance(raw_backend, dict):
            continue
        descriptors.append(
            GenerationBackendDescriptor(
                backend_id=str(raw_backend["id"]),
                module=str(raw_backend["module"]),
                class_name=str(raw_backend["class"]),
                label=str(raw_backend.get("label", "")),
                init_kwargs=dict(raw_backend.get("init_kwargs", {})),
            )
        )
    return tuple(descriptors)


def discover_generation_backend_descriptors(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
) -> tuple[GenerationBackendDescriptor, ...]:
    """Return pack-declared backend descriptors from discovered manifests."""
    descriptors: list[GenerationBackendDescriptor] = []
    for discovered_pack in discover_pack_metadata(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
        discover_packs_fn=discover_packs,
    ):
        descriptors.extend(descriptors_from_pack(discovered_pack.pack))
    return tuple(descriptors)


def load_default_generation_backend_registry(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
) -> GenerationBackendRegistry:
    """Load a registry seeded with built-ins and discovered pack extensions."""
    return GenerationBackendRegistry(
        descriptors=discover_generation_backend_descriptors(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        ),
    )


def _builtin_generation_backend_descriptors() -> tuple[GenerationBackendDescriptor, ...]:
    return (
        GenerationBackendDescriptor(
            backend_id=CLOUD_BACKEND_ID,
            module="astrid.core.generation.backends.fal",
            class_name="FalBackend",
            label="Cloud (fal)",
        ),
        GenerationBackendDescriptor(
            backend_id=CODEX_BACKEND_ID,
            module="astrid.core.generation.backends.codex",
            class_name="CodexBackend",
            label="Codex image_generation",
        ),
        GenerationBackendDescriptor(
            backend_id=LOCAL_BACKEND_ID,
            module="astrid.core.generation.backends.vibecomfy",
            class_name="VibeComfyBackend",
            label="Local (vibecomfy)",
        ),
        GenerationBackendDescriptor(
            backend_id=WAVESPEED_BACKEND_ID,
            module="astrid.core.generation.backends.wavespeed",
            class_name="WavespeedBackend",
            label="Cloud (wavespeed)",
        ),
    )


def _accepts_keyword(factory: type[Any], keyword: str) -> bool:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    if keyword in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
