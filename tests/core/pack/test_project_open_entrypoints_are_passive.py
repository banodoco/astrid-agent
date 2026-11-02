from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from astrid.core.element.registry import load_pack_elements
from astrid.core.pack import source_setup
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.sdk.discovery import _discover_pack_inventory


@pytest.fixture
def missing_project_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    packs_root = project / "astrid" / "packs"
    missing_pack = packs_root / "missing_project_pack"
    missing_pack.mkdir(parents=True)
    marker = tmp_path / "missing-project-pack-executed"
    (missing_pack / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("project-open discovery must not provision, fetch, or launch code")

    monkeypatch.setattr(source_setup, "provision", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    return project, packs_root, str(marker)


@pytest.mark.parametrize(
    ("entrypoint", "id_reader"),
    [
        (
            "core pack discovery",
            lambda project, packs_root: discover_pack_metadata(
                project_root=project,
                extra_pack_roots=(str(packs_root),),
            ),
        ),
        (
            "SDK pack inventory",
            lambda project, packs_root: _discover_pack_inventory(
                project_root=project,
                extra_pack_roots=(str(packs_root),),
            ),
        ),
    ],
)
def test_project_open_pack_entrypoints_do_not_provision_missing_code(
    missing_project_pack: tuple[Path, Path, str],
    entrypoint: str,
    id_reader: Callable[[Path, Path], tuple[Any, ...]],
) -> None:
    project, packs_root, marker = missing_project_pack

    discovered = id_reader(project, packs_root)

    assert "missing_project_pack" not in {pack.id for pack in discovered}, entrypoint
    assert not Path(marker).exists(), entrypoint


def test_project_effect_registry_does_not_import_missing_project_pack(
    missing_project_pack: tuple[Path, Path, str],
) -> None:
    project, packs_root, marker = missing_project_pack

    elements = load_pack_elements(
        project_root=project,
        extra_pack_roots=(str(packs_root),),
    )

    assert all(not element.root.is_relative_to(packs_root) for element in elements)
    assert not Path(marker).exists()
