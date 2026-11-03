from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from astrid.core.pack.discovery import discover_pack_metadata


def test_project_reference_to_missing_pack_does_not_fetch_or_execute(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"

    # This is code that would be present if the referenced pack were fetched
    # or imported. A pack without a manifest is unavailable and must remain
    # inert during project discovery.
    project_pack_root = project / "astrid" / "packs"
    pack_root = project_pack_root / "missing_project_pack"
    pack_root.mkdir(parents=True)
    marker = tmp_path / "pack-executed"
    (pack_root / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("project discovery must not access the network or launch processes")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    packs = discover_pack_metadata(
        project_root=project,
        extra_pack_roots=(str(project_pack_root),),
        discover_packs_fn=lambda *_args: (),
    )

    assert "missing_project_pack" not in {pack.id for pack in packs}
    assert not marker.exists()
