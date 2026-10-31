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
        "vibecomfy-h3-workflow",
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


def test_builtin_and_legacy_positional_construction_have_local_only_placement():
    from astrid.core.generation.engine_contracts import PlacementDescriptor

    expected = PlacementDescriptor("astrid.generation-placement.v1", ("local",), "local", None, False)
    legacy = GenerationBackendDescriptor("legacy", "vendor.module", "Adapter", "Legacy", {"setting": 1})
    assert legacy.placement == expected
    assert legacy.init_kwargs == {"setting": 1}
    registry = GenerationBackendRegistry()
    assert {d.backend_id for d in registry.descriptors()} == {"local", "cloud", "codex", "wavespeed", "vibecomfy-h3-workflow"}
    assert all(d.placement == expected for d in registry.descriptors()
               if d.backend_id != "vibecomfy-h3-workflow")
    workflow = registry.get_descriptor("vibecomfy-h3-workflow")
    assert workflow.placement == PlacementDescriptor(
        "astrid.generation-placement.v1", ("local", "runpod"), "local", "vibecomfy-h3", True
    )


@pytest.mark.parametrize("placement", [None, {}, False, "runpod"])
def test_direct_backend_descriptor_rejects_untyped_placement(placement):
    with pytest.raises(ValueError, match="PlacementDescriptor"):
        GenerationBackendDescriptor("invalid", "never_import", "Adapter", placement=placement)


def test_pack_descriptor_roundtrip_and_discovery_are_inert(tmp_path, monkeypatch):
    import importlib
    import socket
    import subprocess
    from astrid.core.generation.backends.registry import descriptors_from_pack, discover_generation_backend_descriptors
    from astrid.core.generation.engine_contracts import PlacementDescriptor
    from astrid.core.pack import PackDefinition, load_pack_manifest
    from astrid.core.util import generation_helpers
    from astrid.core.util.credentials_scope import CredentialsScope
    import yaml

    def forbidden(*args, **kwargs):
        pytest.fail("discovery attempted backend import, credential access, or network")

    # Backend imports use this seam only on create(); discovery must not use it.
    monkeypatch.setattr(importlib, "import_module", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(generation_helpers, "load_provider_key", forbidden)
    monkeypatch.setattr(CredentialsScope, "resolve_local", forbidden)
    placement = PlacementDescriptor("astrid.generation-placement.v1", ("local", "runpod"), "runpod", "unindexed-engine", True)
    descriptor = GenerationBackendDescriptor("placement-probe", "never_import", "Adapter", "Probe", {"region": "eu"}, placement)
    raw = descriptor.to_dict()
    pack_root = tmp_path / "probe"
    pack_root.mkdir()
    manifest = {"schema_version": 2, "id": "probe", "name": "Probe", "version": "1.0.0",
                "extensions": {"generation": {"backends": [raw]}}}
    (pack_root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    pack = load_pack_manifest(pack_root / "pack.yaml")
    assert pack.extensions["generation"]["backends"][0] == raw
    assert descriptors_from_pack(pack) == (descriptor,)
    found = discover_generation_backend_descriptors(project_root=tmp_path, extra_pack_roots=(str(pack_root),))
    assert descriptor in found
    registry = GenerationBackendRegistry(found)
    assert registry.get_descriptor("placement-probe").placement.resolve() == "runpod"
    assert "never_import" not in sys.modules

    # Even hand-built PackDefinitions must validate before lazy backend creation.
    raw["placement"]["requires_endpoint"] = 1
    pack = PackDefinition("probe", "Probe", "1.0.0", pack_root, pack_root / "pack.yaml", {},
                          extensions={"generation": {"backends": [raw]}})
    with pytest.raises(ValueError, match="boolean"):
        descriptors_from_pack(pack)


def test_fresh_registry_discovery_does_not_load_providers_recipes_or_runtime(tmp_path):
    import subprocess
    import textwrap

    script = textwrap.dedent('''
        import sys
        import importlib.abc
        blocked = (
            "runpod", "runpod_lifecycle", "fal_client", "astrid_runtime",
            "astrid.core.generation.engine_recipe",
            "astrid.core.generation.engine_endpoint",
            "astrid.core.generation.engine_runner",
            "astrid.core.generation.engine_execution",
            "astrid.core.generation.backends.vibecomfy",
            "astrid.core.generation.backends.wavespeed",
        )
        class RefuseSideEffects(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                    raise AssertionError("unexpected discovery import: " + fullname)
        sys.meta_path.insert(0, RefuseSideEffects())
        from astrid.core.generation.backends.registry import GenerationBackendRegistry
        registry = GenerationBackendRegistry()
        assert len(registry.descriptors()) == 5
        for descriptor in registry.descriptors():
            if descriptor.backend_id == "vibecomfy-h3-workflow":
                assert descriptor.placement.resolve(None) == "local"
                assert descriptor.placement.supported == ("local", "runpod")
                assert descriptor.placement.engine == "vibecomfy-h3"
                continue
            assert descriptor.placement.resolve(None) == "local"
            assert descriptor.placement.engine is None
            assert descriptor.placement.requires_endpoint is False
        assert not any(name in sys.modules for name in blocked)
    ''')
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
