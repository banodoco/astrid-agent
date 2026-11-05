"""Keep render DTOs bound to the rendering schema contract."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from astrid.core.rendering import contracts
from astrid.packs.rendering.actions.render import run as render_run


_RENDER_DTO_MODULES = {
    "compositor": "astrid.packs.rendering.rendering.finalizers.compositor.run",
    "ffmpeg_finalizer": "astrid.packs.rendering.rendering.finalizers.ffmpeg.run",
    "ffmpeg_run": "astrid.packs.rendering.rendering.renderers.ffmpeg.run",
    "ffmpeg_support": "astrid.packs.rendering.rendering.renderers.ffmpeg.support",
    "remotion": "astrid.packs.rendering.rendering.renderers.remotion.run",
    "threejs": "astrid.packs.rendering.rendering.renderers.threejs.run",
}


@pytest.mark.parametrize("module_id,module_path", _RENDER_DTO_MODULES.items(), ids=_RENDER_DTO_MODULES)
def test_six_render_dtos_use_integer_render_schema(module_id: str, module_path: str) -> None:
    module = importlib.import_module(module_path)
    source = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported_origins = {
        node.module
        for node in ast.walk(source)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "SCHEMA_VERSION" for alias in node.names)
    }

    assert "astrid.core.rendering.contracts" in imported_origins, module_id
    assert "astrid.pack_api" not in imported_origins, module_id
    assert module.SCHEMA_VERSION is contracts.SCHEMA_VERSION
    assert type(module.SCHEMA_VERSION) is int
    assert module.SCHEMA_VERSION == 1


def test_facade_render_request_schema_is_integer_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class CapturingService:
        request = None

        def render(self, request, **kwargs):
            self.request = request
            return tmp_path / "rendered.mp4"

    service = CapturingService()
    monkeypatch.setattr(render_run, "_default_service", lambda: service)
    timeline_path = tmp_path / "timeline.json"
    assets_path = tmp_path / "assets.json"
    timeline_path.write_text('{"tracks": [], "clips": []}', encoding="utf-8")
    assets_path.write_text('{"assets": {}}', encoding="utf-8")

    render_run.render(timeline_path, assets_path, tmp_path / "rendered.mp4")

    assert type(service.request.schema_version) is int
    assert service.request.schema_version == 1
    assert service.request.schema_version == contracts.SCHEMA_VERSION
