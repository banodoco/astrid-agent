"""Frame captures hand the host renderer only environment it admits."""
import json
from pathlib import Path
from types import SimpleNamespace

from astrid.core.execution import remotion_host_session as host
from astrid.core.rendering.contracts import FrameWindow
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion


class _StopAfterFrameRequest(Exception):
    pass


def test_frame_capture_request_environment_is_admitted_by_host(tmp_path, monkeypatch):
    p = tmp_path / "timeline.json"
    p.write_text(json.dumps({"theme": "banodoco-default", "tracks": [], "clips": []}))
    assets = tmp_path / "assets.json"
    assets.write_text('{"assets":{}}')
    project = tmp_path / "project"
    project.mkdir()
    frame_output = tmp_path / "frames"
    frame_output.mkdir()
    runtime = SimpleNamespace(node_executable=Path("/qualified/node"),
                              remotion_cli=Path("/qualified/remotion-cli.js"),
                              node_version="v20.19.4")
    profile = SimpleNamespace(width=320, height=180, fps_rational=(30000, 1001))
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *a: runtime)
    monkeypatch.setattr(remotion, "_regenerate_element_registries", lambda *a: None)
    monkeypatch.setattr(remotion, "_validate_renderer_effect_registry", lambda *a: None)
    monkeypatch.setattr(remotion, "_effective_registry_state", lambda *a: {"hash": "qualified-registry"})
    monkeypatch.setattr(remotion, "_canonical_profile", lambda *a: profile)
    monkeypatch.setattr(remotion, "_resolved_theme_for_render",
                        lambda *a: {"id": "banodoco-default", "visual": {"canvas": {"width": 320, "height": 180, "fps": 30000 / 1001}}})
    monkeypatch.setattr(remotion, "_stage_effect_assets_for_timeline",
                        lambda *a, **k: {"root": None, "effects": [], "animations": [], "transitions": []})
    monkeypatch.setattr(remotion, "_available_remotion_port", lambda: 32101)
    monkeypatch.delenv("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", raising=False)
    monkeypatch.delenv("ASTRID_PROGRESS_PATH", raising=False)

    seen = []

    class RecordingFrameSession:
        def render(self, **kwargs):
            seen.append(kwargs)
            raise _StopAfterFrameRequest()

    try:
        remotion._execute_remotion_locked(
            p, assets, tmp_path / "output.mp4", provenance_out_path=tmp_path / "output.mp4",
            project_dir=project, composition_id="TimelineComposition", theme_path=None,
            min_free_gb=None, window=None, frame_numbers=[3], frame_output_dir=frame_output,
            frame_resolution=(320, 180), frame_session=RecordingFrameSession(),
            frame_session_identity="regression-identity",
        )
    except _StopAfterFrameRequest:
        pass
    assert len(seen) == 1, "capture never reached the host frame session"

    environment = seen[0]["environment"]
    rejected = set(environment) - host._ADMITTED_RENDER_ENVIRONMENT
    assert not rejected, f"host would reject capture environment keys: {sorted(rejected)}"
