"""T6.5 — four-file renderer scaffold and the internal authoring CLI route.

Locks the exact scaffold contract:

* exactly four files (``pack.yaml``, ``rendering/renderer.yaml``, ``render.py``,
  ``test_renderer.py``) with no placeholder tokens and each file within 50
  nonblank/non-comment lines;
* collision refusal without ``force=True`` and force overwrite;
* static validation of the generated pack/renderer manifests
  (``validate_pack`` + canonical manifest loading);
* the generated ``test_renderer.py`` passes when run on the scaffold output;
* the ``create`` CLI route (``cli.main``) writes to the requested directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.gateway.dispatch import _TOP_LEVEL_HANDLERS
from astrid.core.pack.manifest import load_manifest_mapping
from astrid.core.pack.validate import validate_pack
from astrid.core.rendering.cli import main as renderers_cli_main
from astrid.core.rendering.scaffold import SCAFFOLD_FILES, create_renderer_scaffold

PLACEHOLDER_TOKENS = ("TODO", "FIXME", "XXX", "lorem", "example.com")
# Schema v3 requires the renderer descriptor beneath rendering/.
_EXPECTED_FILES = ["pack.yaml", "render.py", "rendering/renderer.yaml", "test_renderer.py"]


def _nonblank_noncomment_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _scaffold_file_names(dest: Path) -> list[str]:
    return sorted(
        path.relative_to(dest).as_posix()
        for path in dest.rglob("*")
        if path.is_file() and not any(part.startswith((".", "__pycache__")) for part in path.relative_to(dest).parts)
    )


def test_scaffold_writes_exactly_four_files(tmp_path: Path) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")

    assert _scaffold_file_names(dest) == _EXPECTED_FILES
    for filename in SCAFFOLD_FILES:
        assert (dest / filename).is_file()


def test_scaffold_has_no_placeholders_and_respects_line_budgets(
    tmp_path: Path,
) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")

    for filename in SCAFFOLD_FILES:
        text = (dest / filename).read_text(encoding="utf-8")
        lowered = text.lower()
        for token in PLACEHOLDER_TOKENS:
            assert token.lower() not in lowered, f"{filename} contains {token!r}"
        lines = _nonblank_noncomment_lines(text)
        assert len(lines) <= 50, (
            f"{filename} has {len(lines)} nonblank/non-comment lines (max 50)"
        )


def test_scaffold_collision_refused_and_force_overwrites(tmp_path: Path) -> None:
    dest = tmp_path / "wave"
    create_renderer_scaffold("wave", dest)

    with pytest.raises(FileExistsError):
        create_renderer_scaffold("wave", dest)

    # A single colliding scaffold file also refuses without force.
    (dest / "render.py").write_text("# mutated\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_renderer_scaffold("wave", dest)

    create_renderer_scaffold("wave", dest, force=True)
    assert "BACKEND_ID" in (dest / "render.py").read_text(encoding="utf-8")
    assert _scaffold_file_names(dest) == _EXPECTED_FILES


def test_scaffold_refuses_existing_file_destination(tmp_path: Path) -> None:
    dest = tmp_path / "occupied"
    dest.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_renderer_scaffold("wave", dest)


def test_scaffold_static_validation_passes(tmp_path: Path) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")

    errors, _warnings = validate_pack(dest)
    assert not errors, errors

    pack = load_manifest_mapping(dest / "pack.yaml", manifest_kind="pack")
    assert pack["id"] == "wave"
    assert pack["schema_version"] == 3
    assert pack["rendering"] == {"wave": {"type": "renderer", "path": "rendering/renderer.yaml"}}

    manifest = load_manifest_mapping(
        dest / "rendering" / "renderer.yaml",
        manifest_kind="renderer",
    )
    assert manifest["id"] == "wave.wave"
    assert manifest["command"] == ["python3", "render.py"]
    assert manifest["operations"] == ["support", "render"]
    assert manifest["required_permissions"] == ["project_files", "subprocess"]


def test_scaffold_renderer_produces_valid_render_result(tmp_path: Path) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request_path = workspace / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timeline_path": "timeline.json",
                "output_name": "out.mp4",
                "audio": "rendered",
            }
        ),
        encoding="utf-8",
    )
    result_path = workspace / "result.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(dest / "render.py"),
            "render",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr

    from astrid.core.rendering import RenderResult

    result = RenderResult.from_dict(
        json.loads(result_path.read_text(encoding="utf-8"))
    )
    assert result.audio_ownership.value == "rendered"
    assert (workspace / result.video.path).is_file()
    assert len(result.video.sha256) == 64


def test_scaffold_generated_test_passes(tmp_path: Path) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(dest / "test_renderer.py"),
        ],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_create_cli_route_writes_to_requested_directory(tmp_path: Path) -> None:
    dest = tmp_path / "cli_wave"
    assert renderers_cli_main(["create", "wave", str(dest)]) == 0
    assert _scaffold_file_names(dest) == _EXPECTED_FILES

    dispatch_dest = tmp_path / "dispatch_wave"
    assert renderers_cli_main(["create", "wave", str(dispatch_dest)]) == 0
    assert _scaffold_file_names(dispatch_dest) == _EXPECTED_FILES
    assert "renderers" not in _TOP_LEVEL_HANDLERS
