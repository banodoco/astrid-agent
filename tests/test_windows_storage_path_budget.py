"""Windows path budgets for Runtime-owned packed source and CAS files."""

from __future__ import annotations

import re
import stat
from pathlib import Path, PureWindowsPath
import uuid

from astrid.core.execution import generic_host
from astrid.installer import backend as installer_backend
from runtime_protocol.cas import ContentAddressedStore
from runtime_protocol import platform as runtime_platform
from runtime_protocol.store import RealmStore


def _default_windows_data_root(monkeypatch) -> PureWindowsPath:
    """Ask Runtime's Windows root helper using a 40-character user profile."""
    local_app_data = f"C:/Users/{'u' * 40}/AppData/Local"
    with monkeypatch.context() as win:
        win.setattr(runtime_platform.sys, "platform", "win32")
        win.setenv("LOCALAPPDATA", local_app_data)
        return PureWindowsPath(str(runtime_platform.data_root()))


def _assert_windows_budget(path: PureWindowsPath, *, directory: bool) -> None:
    # Windows MAX_PATH and directory-component budget count UTF-16 code units.
    length = len(str(path).encode("utf-16-le")) // 2
    budget = 248 if directory else 260
    assert length < budget, f"Windows {'directory' if directory else 'file'} path is {length}/{budget}: {path}"


def test_bundled_pack_capture_paths_fit_windows_budget(tmp_path, monkeypatch):
    data_root = tmp_path / "runtime-data"
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(data_root))
    pack_root = Path(generic_host.__file__).resolve().parents[2] / "packs"
    bundled_packs = sorted(path for path in pack_root.iterdir()
                           if path.is_dir() and (path / "pack.yaml").is_file())
    assert bundled_packs
    windows_data_root = _default_windows_data_root(monkeypatch)

    # Observe the actual capture directory name and parent from the production
    # writer, then map each pack's real inventory under that same layout.
    first = bundled_packs[0]
    record = type("Record", (), {"definition": type("Definition", (), {"metadata": {
        "source_pack": first.name,
        "pack_root": str(first),
    }})()})()
    generation = generic_host._selected_pack_generation(record)
    with generic_host._portable_pack_capture(generation) as captured:
        snapshot = captured[2]
        capture_parent = snapshot.parent
        assert capture_parent.parent == data_root / "runtime" / "pack-captures"
        assert re.fullmatch(r"[0-9a-f]{32}", capture_parent.name)

        windows_capture_root = (
            windows_data_root / "runtime" / "pack-captures" / capture_parent.name
        )
        for pack in bundled_packs:
            inventory = generic_host._pack_capture_inventory(pack)
            windows_pack_root = windows_capture_root / pack.name
            for source_path, identity in inventory.items():
                mapped = windows_pack_root.joinpath(*source_path.relative_to(pack).parts)
                is_directory = stat.S_ISDIR(identity[2])
                checked = mapped if is_directory else mapped.parent
                _assert_windows_budget(checked, directory=True)
                if not is_directory:
                    _assert_windows_budget(mapped, directory=False)


def test_default_runtime_cas_paths_fit_windows_budget(tmp_path, monkeypatch):
    generated = "01234567-89ab-cdef-0123-456789abcdef"
    data_root = tmp_path / "data"
    captured_argv = []
    backend = installer_backend.Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")

    def fake_run(argv, **_kwargs):
        captured_argv.extend(str(item) for item in argv)
        return '{"ok":true}'

    with monkeypatch.context() as create:
        create.setattr(installer_backend.uuid, "uuid4", lambda: uuid.UUID(generated))
        create.setattr(backend, "run", fake_run)
        backend.create(None, data_root)

    actual_realm_root = Path(captured_argv[captured_argv.index("--realm-root") + 1])
    realm_id = captured_argv[captured_argv.index("--realm-id") + 1]
    assert realm_id == generated
    assert actual_realm_root == data_root / "runtime" / "realms" / generated
    windows_data_root = _default_windows_data_root(monkeypatch)
    realm_root = windows_data_root.joinpath(*actual_realm_root.relative_to(data_root).parts)
    assert realm_root == windows_data_root / "runtime" / "realms" / generated

    actual_realm = data_root / "runtime" / "realms" / realm_id
    store = RealmStore.initialize(actual_realm, display_name="Path budget", realm_id=realm_id)
    try:
        cas = ContentAddressedStore(store.cas_root)
        object_path = cas.path_for("a" * 64)
        mapped_cas_root = realm_root.joinpath(*store.cas_root.relative_to(actual_realm).parts)
        mapped_object_path = mapped_cas_root.joinpath(*object_path.relative_to(store.cas_root).parts)
        assert mapped_object_path == realm_root / "cas" / "sha256" / ("a" * 2) / ("a" * 62)
        _assert_windows_budget(mapped_object_path.parent, directory=True)
        _assert_windows_budget(mapped_object_path, directory=False)

        staged = cas.stage((b"path budget",), store.staging_root)
        try:
            mapped_stage_path = realm_root.joinpath(*staged.path.relative_to(actual_realm).parts)
            assert re.fullmatch(r"[0-9a-f]{32}\.upload", mapped_stage_path.name)
            _assert_windows_budget(mapped_stage_path.parent, directory=True)
            _assert_windows_budget(mapped_stage_path, directory=False)
        finally:
            staged.discard()
    finally:
        store.close()
