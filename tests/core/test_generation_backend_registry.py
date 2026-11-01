"""Tests for the generation backend registry."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from astrid.core.generation.backends.fal import FalBackend
from astrid.core.generation.backends.registry import (
    GenerationBackendDescriptor,
    GenerationBackendRegistry,
)
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend


def test_registry_lists_builtins_without_importing_third_party_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "fake_generation_backend_registry_list_probe"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "raise RuntimeError('module should not be imported during listing')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)

    registry = GenerationBackendRegistry(
        descriptors=(
            GenerationBackendDescriptor(
                backend_id="third-party",
                module=module_name,
                class_name="FakeBackend",
            ),
        )
    )

    descriptors = registry.descriptors()

    assert [descriptor.backend_id for descriptor in descriptors] == [
        "cloud",
        "codex",
        "local",
        "third-party",
        "wavespeed",
    ]
    assert module_name not in sys.modules


def test_registry_creates_builtin_backends_via_standard_factory_signature(
    tmp_path: Path,
) -> None:
    registry = GenerationBackendRegistry()
    env_file = tmp_path / ".env"

    local_backend = registry.create("local", env_file=env_file)
    cloud_backend = registry.create("cloud", env_file=env_file)
    codex_backend = registry.create("codex", env_file=env_file)
    wavespeed_backend = registry.create("wavespeed", env_file=env_file)

    assert isinstance(local_backend, VibeComfyBackend)
    assert isinstance(cloud_backend, FalBackend)
    from astrid.core.generation.backends.codex import CodexBackend
    from astrid.core.generation.backends.wavespeed import WavespeedBackend

    assert isinstance(codex_backend, CodexBackend)
    assert isinstance(wavespeed_backend, WavespeedBackend)
    assert cloud_backend._env_file == env_file
    assert wavespeed_backend._env_file == env_file


def test_registry_rejects_duplicate_backend_ids() -> None:
    with pytest.raises(ValueError, match="duplicate generation backend id 'local'"):
        GenerationBackendRegistry(
            descriptors=(
                GenerationBackendDescriptor(
                    backend_id="local",
                    module="astrid.core.generation.backends.fal",
                    class_name="FalBackend",
                ),
            )
        )


def test_registry_lazily_imports_selected_third_party_backend_only_on_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "fake_generation_backend_registry_create_probe"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "from astrid.core.generation.backends.base import BackendAdapter, GenerationResult",
                "CREATED = []",
                "class FakeBackend(BackendAdapter):",
                "    def __init__(self, env_file=None, setting='base', extra='descriptor'):",
                "        CREATED.append({'env_file': env_file, 'setting': setting, 'extra': extra})",
                "        self.env_file = env_file",
                "        self.setting = setting",
                "        self.extra = extra",
                "    def generate(self, entry, mode, params, out_dir: Path) -> GenerationResult:",
                "        raise NotImplementedError",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)

    registry = GenerationBackendRegistry(
        descriptors=(
            GenerationBackendDescriptor(
                backend_id="third-party",
                module=module_name,
                class_name="FakeBackend",
                init_kwargs={"setting": "descriptor", "extra": "descriptor"},
            ),
        )
    )

    assert module_name not in sys.modules
    registry.create("cloud")
    assert module_name not in sys.modules

    env_file = tmp_path / ".env"
    backend = registry.create(
        "third-party",
        env_file=env_file,
        init_overrides={"setting": "override"},
    )

    imported_module = sys.modules[module_name]
    assert module_name in sys.modules
    assert imported_module.CREATED == [
        {"env_file": env_file, "setting": "override", "extra": "descriptor"}
    ]
    assert backend.env_file == env_file
    assert backend.setting == "override"
    assert backend.extra == "descriptor"


# ---------------------------------------------------------------------------
# T9: Additional registry unit tests
# ---------------------------------------------------------------------------


def test_builtin_descriptors_have_correct_metadata() -> None:
    """Built-in descriptors expose the expected ids, module paths, class names,
    and labels without requiring backend instantiation."""
    registry = GenerationBackendRegistry()

    builtin_ids = {"cloud", "codex", "local"}
    seen: set[str] = set()
    for descriptor in registry.descriptors():
        if descriptor.backend_id in builtin_ids:
            seen.add(descriptor.backend_id)

        if descriptor.backend_id == "cloud":
            assert descriptor.module == "astrid.core.generation.backends.fal"
            assert descriptor.class_name == "FalBackend"
            assert descriptor.label == "Cloud (fal)"
        elif descriptor.backend_id == "codex":
            assert descriptor.module == "astrid.core.generation.backends.codex"
            assert descriptor.class_name == "CodexBackend"
            assert descriptor.label == "Codex image_generation"
        elif descriptor.backend_id == "local":
            assert descriptor.module == "astrid.core.generation.backends.vibecomfy"
            assert descriptor.class_name == "VibeComfyBackend"
            assert descriptor.label == "Local (vibecomfy)"
        elif descriptor.backend_id == "wavespeed":
            assert descriptor.module == "astrid.core.generation.backends.wavespeed"
            assert descriptor.class_name == "WavespeedBackend"
            assert descriptor.label == "Cloud (wavespeed)"

    assert seen == builtin_ids, f"built-in descriptors missing: {builtin_ids - seen}"


def test_register_method_adds_descriptor() -> None:
    """Direct ``register()`` adds a synthetic descriptor that appears in
    ``descriptors()`` and ``get_descriptor()``."""
    registry = GenerationBackendRegistry(descriptors=())
    # Start empty (no built-ins seeded)
    registry._descriptors.clear()

    desc = GenerationBackendDescriptor(
        backend_id="synthetic",
        module="some.module",
        class_name="SomeClass",
        label="Synthetic",
        init_kwargs={"timeout": 30},
    )
    registry.register(desc)

    assert registry.get_descriptor("synthetic") is desc
    assert desc in registry.descriptors()


def test_register_many_adds_all_descriptors() -> None:
    """``register_many()`` registers every descriptor and preserves ordering
    in the internal map."""
    registry = GenerationBackendRegistry(descriptors=())
    registry._descriptors.clear()

    desc_a = GenerationBackendDescriptor(
        backend_id="a", module="ma", class_name="A"
    )
    desc_b = GenerationBackendDescriptor(
        backend_id="b", module="mb", class_name="B"
    )
    desc_c = GenerationBackendDescriptor(
        backend_id="c", module="mc", class_name="C"
    )

    registry.register_many([desc_a, desc_b, desc_c])

    ids = [d.backend_id for d in registry.descriptors()]
    assert ids == ["a", "b", "c"]


def test_register_rejects_duplicate_id() -> None:
    """Calling ``register()`` with an already-known ``backend_id`` raises
    ``ValueError``."""
    registry = GenerationBackendRegistry(descriptors=())
    registry._descriptors.clear()

    desc = GenerationBackendDescriptor(
        backend_id="only", module="m", class_name="C"
    )
    registry.register(desc)

    dup = GenerationBackendDescriptor(
        backend_id="only", module="other", class_name="Other"
    )
    with pytest.raises(ValueError, match="duplicate generation backend id 'only'"):
        registry.register(dup)


def test_get_descriptor_returns_correct_metadata() -> None:
    """``get_descriptor()`` returns the descriptor with all fields intact."""
    registry = GenerationBackendRegistry()

    cloud_desc = registry.get_descriptor("cloud")
    assert cloud_desc.backend_id == "cloud"
    assert cloud_desc.module == "astrid.core.generation.backends.fal"
    assert cloud_desc.class_name == "FalBackend"
    assert isinstance(cloud_desc.init_kwargs, dict)
    assert cloud_desc.init_kwargs == {}


def test_get_descriptor_raises_keyerror_for_unknown_id() -> None:
    """``get_descriptor()`` raises ``KeyError`` with a message listing
    available backends."""
    registry = GenerationBackendRegistry()

    with pytest.raises(KeyError, match="unknown generation backend 'nonexistent'"):
        registry.get_descriptor("nonexistent")


def test_get_descriptor_does_not_import_third_party_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_descriptor()`` returns the inert descriptor without importing
    the module it references."""
    module_name = "fake_get_descriptor_probe"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "raise RuntimeError('should not import during get_descriptor')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)

    registry = GenerationBackendRegistry(
        descriptors=(
            GenerationBackendDescriptor(
                backend_id="probe",
                module=module_name,
                class_name="FakeBackend",
            ),
        )
    )

    assert module_name not in sys.modules
    desc = registry.get_descriptor("probe")
    assert desc.backend_id == "probe"
    assert module_name not in sys.modules


def test_descriptors_returns_sorted_tuple() -> None:
    """``descriptors()`` returns a ``tuple`` sorted by ``backend_id``."""
    registry = GenerationBackendRegistry()

    result = registry.descriptors()
    assert isinstance(result, tuple)
    ids = [d.backend_id for d in result]
    assert ids == sorted(ids)


def test_descriptor_dataclass_is_frozen() -> None:
    """``GenerationBackendDescriptor`` is frozen — attributes cannot be
    mutated after construction."""
    desc = GenerationBackendDescriptor(
        backend_id="test",
        module="test.module",
        class_name="TestClass",
        label="Test",
    )
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        desc.backend_id = "changed"  # type: ignore[misc]


def test_synthetic_descriptor_stores_all_fields() -> None:
    """A ``GenerationBackendDescriptor`` constructed directly preserves all
    fields including optional ``label`` and ``init_kwargs``."""
    desc = GenerationBackendDescriptor(
        backend_id="full",
        module="full.module",
        class_name="FullClass",
        label="Full Label",
        init_kwargs={"key_a": 1, "key_b": "val"},
    )
    assert desc.backend_id == "full"
    assert desc.module == "full.module"
    assert desc.class_name == "FullClass"
    assert desc.label == "Full Label"
    assert desc.init_kwargs == {"key_a": 1, "key_b": "val"}


def test_synthetic_descriptor_defaults_label_and_init_kwargs() -> None:
    """Omitted optional fields default to empty string and empty dict."""
    desc = GenerationBackendDescriptor(
        backend_id="minimal",
        module="min.module",
        class_name="MinClass",
    )
    assert desc.label == ""
    assert desc.init_kwargs == {}
    assert desc.backend_id == "minimal"
