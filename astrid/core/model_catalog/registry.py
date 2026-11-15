"""Model registry — loads models.yaml, validates, and provides lookup.

Uses ``yaml.safe_load`` (stdlib PyYAML) because the YAML subset parser
(``_parse_yaml_subset``) does not tolerate flow-style inline mappings in
``param_map`` blocks.

Schema v2: model → mode → backend taxonomy.  Lookups are keyed by
(model_id, mode) rather than model_id alone.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.model_catalog.schema import (
    LoraEntry,
    ModelEntry,
    ModeSpec,
    validate_lora_registry,
    validate_registry_with_backends,
)

_default_cache: dict[tuple, ModelRegistry] = {}


def _reset_for_tests() -> None:
    _default_cache.clear()


class ModelRegistry:
    """In-memory registry of generation model entries.

    Loaded from the shipped ``models.yaml`` via :meth:`load_default`.
    Schema v2 — every lookup that cares about generation capability
    must specify both *model_id* and *mode*.
    """

    def __init__(self, entries: list[ModelEntry]) -> None:
        self._by_id: dict[str, ModelEntry] = {e.id: e for e in entries}

    # -- lookup ----------------------------------------------------------

    def get(self, model_id: str) -> ModelEntry:
        """Return the :class:`ModelEntry` for *model_id*.

        Raises:
            KeyError: If *model_id* is not registered.
        """
        try:
            return self._by_id[model_id]
        except KeyError:
            available = ", ".join(sorted(self._by_id))
            raise KeyError(
                f"Unknown model {model_id!r}. Available: {available}"
            ) from None

    def get_by_mode(self, model_id: str, mode: str) -> tuple[ModelEntry, ModeSpec]:
        """Return ``(ModelEntry, ModeSpec)`` for a specific *(model_id, mode)*.

        Raises:
            KeyError: If *model_id* is unknown or *mode* is not supported
                by that model.
        """
        entry = self.get(model_id)
        if mode not in entry.modes:
            available = ", ".join(sorted(entry.modes))
            raise KeyError(
                f"Model {model_id!r} does not support mode {mode!r}. "
                f"Available modes: {available}"
            )
        return entry, entry.modes[mode]

    def backend_available(self, model_id: str, mode: str, execution: str) -> bool:
        """Return ``True`` if *execution* backend is available for *(model_id, mode)*.

        The method remains a pure membership check against the validated
        backend ids declared for the selected mode.
        """
        _, mode_spec = self.get_by_mode(model_id, mode)
        return execution in mode_spec.backends

    def list_by_modality(
        self, modality: str, *, include_closed: bool = False
    ) -> list[ModelEntry]:
        """Return all entries belonging to *modality* (e.g. ``"image"``).

        Closed-weight models (``closed: true``) are hidden by default;
        pass ``include_closed=True`` to include them.
        """
        entries = [e for e in self._by_id.values() if e.modality == modality]
        if not include_closed:
            entries = [e for e in entries if not e.closed]
        return entries

    def list_all(self, *, include_closed: bool = False) -> list[ModelEntry]:
        """Return every registered entry.

        Closed-weight models (``closed: true``) are hidden by default;
        pass ``include_closed=True`` to include them.
        """
        entries = list(self._by_id.values())
        if not include_closed:
            entries = [e for e in entries if not e.closed]
        return entries

    # -- loading ---------------------------------------------------------

    @classmethod
    def load_default(
        cls,
        *,
        project_root: str | Path = REPO_ROOT,
        extra_pack_roots: tuple[str, ...] = (),
    ) -> ModelRegistry:
        """Load the shipped ``models.yaml`` from the catalog directory."""
        yaml_path = Path(__file__).resolve().parent / "models.yaml"
        if not yaml_path.is_file():
            raise AstridError(
                f"model registry not found: {yaml_path}",
                recovery_command="reinstall astrid; models.yaml ships with the package",
            )
        # Runtime assembly seam. ``load_default`` is a convenience assembler that
        # builds a fully-populated registry by pack-scanning — and pack-scanning
        # for generation features/backends lives in ``generation`` (which sits
        # *above* ``model_catalog``: backends depend on the model schema, not the
        # reverse). So this one call reaches upward at *call time only*; the
        # import graph stays acyclic at import time. Resolved via ``importlib`` to
        # keep that property explicit. (Proper home for this assembler is the
        # registry-collapse layer — see W7 in the restructure plan.)
        import importlib

        load_default_generation_backend_registry = getattr(
            importlib.import_module("astrid.core.generation.backends.registry"),
            "load_default_generation_backend_registry",
        )
        load_default_generation_taxonomy_registry = getattr(
            importlib.import_module("astrid.core.generation.features"),
            "load_default_generation_taxonomy_registry",
        )

        from astrid.core.pack._registry_cache import discovery_key
        from astrid.core.pack.discovery import pack_discovery_scope

        features = importlib.import_module("astrid.core.generation.features")
        backends = importlib.import_module("astrid.core.generation.backends.registry")

        def key():
            # Both assemblers use the same discovery inputs. Preserve custom
            # scanner seams: their arbitrary state has no content identity.
            if features.discover_packs is not backends.discover_packs:
                return None
            fingerprint = discovery_key(project_root, extra_pack_roots, features.discover_packs)
            if fingerprint is None:
                return None
            return (cls, fingerprint, str(yaml_path), yaml_path.read_bytes(),
                    load_default_generation_taxonomy_registry,
                    load_default_generation_backend_registry)

        before = key()
        if before is not None and before in _default_cache:
            return deepcopy(_default_cache[before])
        # Keep taxonomy and backend admission on the existing content-verified
        # discovery seam, even outside a generate invocation.
        with pack_discovery_scope():
            result = cls._load_default_uncached(
                yaml_path, project_root, extra_pack_roots,
                load_default_generation_taxonomy_registry,
                load_default_generation_backend_registry,
            )
        if before is not None and before == key():
            _default_cache[before] = deepcopy(result)
        return result

    @classmethod
    def _load_default_uncached(cls, yaml_path, project_root, extra_pack_roots,
                               load_default_generation_taxonomy_registry,
                               load_default_generation_backend_registry):
        raw = _load_yaml(yaml_path)
        taxonomy_registry = load_default_generation_taxonomy_registry(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        )
        backend_registry = load_default_generation_backend_registry(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        )
        entries = validate_registry_with_backends(
            raw,
            allowed_backend_ids=tuple(
                descriptor.backend_id
                for descriptor in backend_registry.descriptors()
            ),
            taxonomy_registry=taxonomy_registry,
        )
        return cls(entries)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file using ``yaml.safe_load``.

    This is the fallback parser chosen over ``_parse_yaml_subset`` because
    flow-style inline mappings (``{key: value}``) in
    ``param_map`` blocks cannot be parsed by the YAML subset parser.
    """
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: expected a YAML mapping at top level, "
            f"got {type(data).__name__}"
        )
    return data


class LoraRegistry:
    """In-memory registry of LoRA adapter entries.

    Loaded from the shipped ``loras.yaml`` via :meth:`load_default`.
    """

    def __init__(self, entries: list[LoraEntry]) -> None:
        self._by_id: dict[str, LoraEntry] = {e.id: e for e in entries}

    # -- lookup ----------------------------------------------------------

    def get(self, lora_id: str) -> LoraEntry:
        """Return the :class:`LoraEntry` for *lora_id*.

        Raises:
            KeyError: If *lora_id* is not registered.
        """
        try:
            return self._by_id[lora_id]
        except KeyError:
            available = ", ".join(sorted(self._by_id))
            raise KeyError(
                f"Unknown LoRA {lora_id!r}. Available: {available}"
            ) from None

    def list_by_base_model(self, base_model: str) -> list[LoraEntry]:
        """Return all LoRAs designed for *base_model*."""
        return [e for e in self._by_id.values() if e.base_model == base_model]

    def list_all(self) -> list[LoraEntry]:
        """Return every registered LoRA entry."""
        return list(self._by_id.values())

    # -- loading ---------------------------------------------------------

    @classmethod
    def load_default(
        cls,
        *,
        model_ids: frozenset[str],
    ) -> LoraRegistry:
        """Load the shipped ``loras.yaml`` from the catalog directory.

        Parameters:
            model_ids: Set of valid model IDs (accepted but base-model
                cross-check is deferred to generation-time in fal.py;
                use :func:`validate_lora_registry` directly for
                load-time cross-check).
        """
        yaml_path = Path(__file__).resolve().parent / "loras.yaml"
        if not yaml_path.is_file():
            raise AstridError(
                f"lora registry not found: {yaml_path}",
                recovery_command="reinstall astrid; loras.yaml ships with the package",
            )
        raw = _load_yaml(yaml_path)
        # Load all loras; base_model cross-check is done by the caller
        # (fal.py) at generation time, not at registry-load time.
        entries = validate_lora_registry(raw, model_ids=None)
        return cls(entries)
