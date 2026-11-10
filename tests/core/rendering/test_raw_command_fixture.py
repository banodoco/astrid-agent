"""T2.2 — committed raw-command fixture pack, protocol v1 end to end.

The fixture at ``tests/fixtures/renderer_packs/raw_command/`` is a trusted
source pack whose backend is a plain stdlib script: it parses argv, reads
``--request`` JSON, and writes ``--result`` JSON without importing the Astrid
SDK, without ffmpeg, and without touching the Astrid ledger (no ``run.json``).

These tests lock the pack's static discovery surface (no code import), drive
both ``render`` and ``support`` through :class:`CommandTransport`, verify the
generated artifact (real sha256, duration, workspace containment), assert no
``run.json`` is ever created, and prove the pack works from an explicit extra
pack root.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from astrid.core.foundation.hash import sha256_file
from astrid.core.pack import discover_packs, load_pack_manifest
from astrid.core.pack.validate import validate_pack
from astrid.core.rendering import RenderResult, SupportReport
from astrid.core.rendering import registry as rendering_registry_module
from astrid.core.rendering.registry import load_default_registries
from astrid.core.rendering.transport import CommandTransport


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "renderer_packs" / "raw_command"
PACK_ROOT = FIXTURE_ROOT
REQUESTS_DIR = FIXTURE_ROOT / "requests"
PACK_ID = "raw_command"
BACKEND_ID = "raw_command.renderer"
ALIAS_ID = "raw_command.legacy"
RENDER_WINDOW_FRAMES = 48  # render.json: [0, 48) @ 24fps == ~2 seconds


# ---------------------------------------------------------------------------
# Discovery helpers (mirror test_registry_matrix.py)
# ---------------------------------------------------------------------------


def _scanner(source_root: Path):
    def scan(root: str | Path | None = None):
        return discover_packs(source_root if root is None else root)

    return scan


@contextmanager
def _load_with_source(
    project_root: Path,
    source_root: Path,
    *,
    extra_pack_roots: tuple[str, ...] = (),
):
    with (
        mock.patch.object(
            rendering_registry_module,
            "discover_packs",
            side_effect=_scanner(source_root),
        ),
        mock.patch.dict(os.environ, {"ASTRID_PACKS_PATH": ""}, clear=False),
    ):
        yield load_default_registries(
            project_root,
            extra_pack_roots=extra_pack_roots,
        )


def _copy_pack(dest_root: Path) -> Path:
    """Copy the committed fixture pack under *dest_root* (pack dir name == id)."""
    dest_root.mkdir(parents=True, exist_ok=True)
    return shutil.copytree(PACK_ROOT, dest_root / PACK_ID)


def _write_request(workspace: Path, request_name: str) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    request = json.loads((REQUESTS_DIR / request_name).read_text(encoding="utf-8"))
    request_path = workspace / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    timeline = REQUESTS_DIR / "timeline.json"
    if timeline.is_file():
        shutil.copyfile(timeline, workspace / "timeline.json")
    return request_path


def _run_transport(
    workspace: Path,
    cwd: Path,
    *,
    verb: str,
    request_name: str = "render.json",
    timeout: float = 30,
):
    request_path = _write_request(workspace, request_name)
    result_path = workspace / "result.json"
    transport = CommandTransport(BACKEND_ID, termination_grace=0.15)
    value = transport.run(
        verb,
        [sys.executable, "backend.py"],
        request_path=request_path,
        result_path=result_path,
        cwd=cwd,
        timeout=timeout,
    )
    return transport, value, workspace


def _assert_clean_render(result: RenderResult, workspace: Path) -> None:
    """Shared artifact assertions for a successful render invocation."""
    assert isinstance(result, RenderResult)
    assert result.schema_version == 1
    assert result.audio_ownership == "rendered"
    assert result.video.audio == "rendered"
    assert result.video.duration_frames == RENDER_WINDOW_FRAMES
    assert result.video.path == "outputs/raw_command.mp4"
    assert BACKEND_ID in result.backend_fragments

    video_path = workspace / result.video.path
    assert video_path.is_file()
    assert video_path.stat().st_size > 0
    assert len(result.video.sha256) == 64
    assert sha256_file(video_path) == result.video.sha256

    profile = result.video.profile
    assert profile.width == 1920
    assert profile.height == 1080
    assert profile.fps_rational == (24, 1)
    assert profile.time_base == (1, 12288)
    assert profile.container == "mp4"
    assert profile.video_codec == "h264"
    assert profile.pixel_format == "yuv420p"
    assert profile.audio_codec == "pcm_s16le"
    assert profile.audio_sample_rate == 48000
    assert profile.audio_channel_layout == "stereo"


# ---------------------------------------------------------------------------
# Static discovery / validation (no code import)
# ---------------------------------------------------------------------------


def test_fixture_pack_validates_and_inspects_without_importing_backend(
    tmp_path: Path,
) -> None:
    errors, _warnings = validate_pack(str(PACK_ROOT))
    assert not errors, errors

    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")
    assert pack.id == PACK_ID
    permission_ids = {permission.id for permission in pack.permissions}
    assert permission_ids == {"subprocess", "project_files"}
    assert all(permission.reason for permission in pack.permissions)
    assert pack.schema_version == "3"
    assert pack.rendering == {"renderer": {"type": "renderer", "path": "rendering/renderer.yaml"}}
    assert pack.aliases == (
        {"kind": "renderer", "alias": ALIAS_ID, "canonical_id": BACKEND_ID},
    )

    source_root = tmp_path / "source"
    _copy_pack(source_root)
    modules_before = set(sys.modules)
    with (
        mock.patch.object(
            importlib,
            "import_module",
            side_effect=AssertionError("backend import"),
        ),
        mock.patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("backend execution"),
        ),
        _load_with_source(tmp_path / "project", source_root) as (renderers, _, _),
    ):

        candidate = renderers.get(BACKEND_ID)
        assert candidate.id == BACKEND_ID
        assert candidate.source_kind == "source"
        assert candidate.execution_eligible is True
        assert candidate.manifest.name == "Raw Command Fixture Renderer"
        assert candidate.manifest.protocol_version == 1
        assert candidate.manifest.operations == ("render", "support")
        assert candidate.manifest.command == ("python3", "backend.py")
        assert candidate.manifest.required_permissions == ("subprocess", "project_files")

        caps = candidate.manifest.capabilities
        assert "media" in caps["clip_types"]
        assert {"visual", "audio"} <= set(caps["track_types"])
        assert caps["features"] == {
            "media": True,
            "audio_mode": "rendered",
            "deterministic": True,
        }
        assert caps["supports_full_timeline"] is True
        assert caps["supports_windows"] is True
        assert caps["output_profiles"] == ["video/mp4"]
        assert caps["audio_ownership"] == ["rendered"]

        # Trusted source-pack alias resolves to the canonical renderer.
        alias = renderers.get(ALIAS_ID)
        assert alias.id == BACKEND_ID
        assert alias.execution_eligible is True

        evidence = renderers.resolve_evidence(ALIAS_ID)
        assert evidence["resolved_id"] == BACKEND_ID
        assert evidence["alias_chain"] == [ALIAS_ID, BACKEND_ID]
        assert evidence["eligible"] is True

        assert len(renderers.candidates(eligible=True)) == 1

    modules_after = set(sys.modules)
    new_modules = modules_after - modules_before
    source_str = str(source_root.resolve())
    for name in new_modules:
        module = sys.modules.get(name)
        module_file = getattr(module, "__file__", None)
        assert module_file is None or not str(Path(module_file).resolve()).startswith(
            source_str
        ), f"module {name!r} is backed by the fixture pack: {module_file}"


# ---------------------------------------------------------------------------
# Protocol verbs through CommandTransport
# ---------------------------------------------------------------------------


def test_render_verb_via_command_transport(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    transport, result, _ = _run_transport(workspace, PACK_ROOT, verb="render")

    _assert_clean_render(result, workspace)
    assert transport.last_logs == {"stdout": "", "stderr": ""}

    # The fixture output must pass STRICT artifact validation against the
    # request profile (dimensions, FPS, codecs, pixel format, audio).
    from astrid.core.rendering.artifacts import validate_render_result
    from astrid.core.rendering.contracts import RenderRequest

    request = json.loads(
        (PACK_ROOT / "requests" / "render.json").read_text(encoding="utf-8")
    )
    parsed_request = RenderRequest.from_dict(request)
    video_abs = workspace / result.video.path
    validate_render_result(
        result,
        expected_profile=parsed_request.profile,
        workspace_root=workspace,
    )
    assert video_abs.is_file()

    # Determinism: a second invocation produces byte-identical media.
    second_workspace = tmp_path / "workspace-2"
    _, second_result, _ = _run_transport(second_workspace, PACK_ROOT, verb="render")
    first_bytes = (workspace / result.video.path).read_bytes()
    second_bytes = (second_workspace / second_result.video.path).read_bytes()
    assert first_bytes == second_bytes
    assert result.video.sha256 == second_result.video.sha256


def test_support_rejects_audio_none_even_with_null_profile(tmp_path: Path) -> None:
    """A request for audio='none' with profile=null is unsupported: the
    renderer always produces rendered PCM stereo audio."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    request_path = workspace / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "output_name": "raw_command.mp4",
                "audio": "none",
                "profile": None,
            }
        ),
        encoding="utf-8",
    )
    result_path = workspace / "result.json"
    transport = CommandTransport(BACKEND_ID, termination_grace=0.15)
    report = transport.run(
        "support",
        [sys.executable, "backend.py"],
        request_path=request_path,
        result_path=result_path,
        cwd=PACK_ROOT,
        timeout=30,
    )
    assert report.supported is False
    assert report.features == {"media": False, "audio_mode": "none"}


def test_support_verb_via_command_transport(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _, report, _ = _run_transport(workspace, PACK_ROOT, verb="support", request_name="support.json")

    assert isinstance(report, SupportReport)
    assert report.schema_version == 1
    assert report.supported is True
    assert report.reasons == []
    assert report.features == {"media": True, "audio_mode": "rendered"}
    assert report.alternatives == []
    assert report.backend == BACKEND_ID
    assert report.backend_version == "1.0.0"


def test_render_and_support_never_create_run_json(tmp_path: Path) -> None:
    _run_transport(tmp_path / "workspace-render", PACK_ROOT, verb="render")
    _run_transport(
        tmp_path / "workspace-support",
        PACK_ROOT,
        verb="support",
        request_name="support.json",
    )

    for root in (tmp_path, PACK_ROOT):
        assert list(root.rglob("run.json")) == [], f"run.json found under {root}"


# ---------------------------------------------------------------------------
# Extra pack root resolution
# ---------------------------------------------------------------------------


def test_fixture_works_from_explicit_extra_pack_root(tmp_path: Path) -> None:
    extra_root = tmp_path / "extra"
    extra_pack = _copy_pack(extra_root)
    empty_source = tmp_path / "empty-source"
    empty_source.mkdir()

    with (
        mock.patch.object(
            rendering_registry_module,
            "discover_packs",
            side_effect=_scanner(empty_source),
        ),
        mock.patch.dict(os.environ, {"ASTRID_PACKS_PATH": ""}, clear=False),
    ):
        renderers, _, _ = load_default_registries(
            tmp_path / "project",
            extra_pack_roots=(str(extra_root),),
        )

    candidate = renderers.get(BACKEND_ID)
    assert candidate.source_kind == "extra"
    assert candidate.execution_eligible is True

    _, result, workspace = _run_transport(tmp_path / "workspace-extra", extra_pack, verb="render")
    _assert_clean_render(result, workspace)
