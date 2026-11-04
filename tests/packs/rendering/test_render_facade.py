"""Facade-boundary tests for ``rendering.render`` after the T4.2 rework.

The facade keeps the ``rendering.render`` capability id and delegates all
dispatch to :class:`RenderService`. These tests pin the delegation surface
without spawning any media tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.rendering.contracts import SCHEMA_VERSION as RENDER_SCHEMA_VERSION
from astrid.core.rendering.contracts import RenderRequest
from astrid.core.rendering.errors import RendererProtocolError, raise_unsupported_error
from astrid.packs.rendering.actions.render import run as render_run


class _FakeService:
    def __init__(self, sentinel: Path | None = None) -> None:
        self.sentinel = sentinel
        self.calls: list[tuple[tuple, dict]] = []

    def render(self, *args, **kwargs) -> Path:
        self.calls.append((args, kwargs))
        if self.sentinel is not None:
            return self.sentinel
        raise AssertionError("unexpected service render call")


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeService:
    fake = _FakeService(tmp_path / "sentinel.mp4")
    monkeypatch.setattr(render_run, "_default_service", lambda: fake)
    return fake


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    timeline = tmp_path / "hype.timeline.json"
    assets = tmp_path / "hype.assets.json"
    out = tmp_path / "out" / "hype.mp4"
    timeline.write_text('{"tracks": [], "clips": []}', encoding="utf-8")
    assets.write_text('{"assets": {}}', encoding="utf-8")
    return timeline, assets, out


def test_render_delegates_to_service_with_default_selector(fake_service: _FakeService, tmp_path: Path) -> None:
    timeline, assets, out = _inputs(tmp_path)
    sentinel = tmp_path / "sentinel.mp4"
    fake_service.sentinel = sentinel

    result = render_run.render(timeline, assets, out)

    assert result == sentinel
    assert len(fake_service.calls) == 1
    (call_args, call_kwargs) = fake_service.calls[0]
    assert len(call_args) == 1
    request = call_args[0]
    assert isinstance(request, RenderRequest)
    assert request.timeline_path == str(timeline.resolve())
    assert request.assets_registry_path == str(assets.resolve())
    assert request.output_name == out.name
    assert call_kwargs["selector"] == "rendering.remotion"
    assert call_kwargs["out_path"] == out
    assert call_kwargs["previous_outputs"] == ()
    # Facade defaults map onto the canonical backend namespace.
    assert request.backend_config["rendering.remotion"] == {
        "composition_id": "TimelineComposition"
    }


def test_render_request_uses_render_contract_schema_version(
    fake_service: _FakeService, tmp_path: Path
) -> None:
    timeline, assets, out = _inputs(tmp_path)

    render_run.render(timeline, assets, out)

    request = fake_service.calls[0][0][0]
    assert render_run.RENDER_SCHEMA_VERSION == RENDER_SCHEMA_VERSION == 1
    assert type(request.schema_version) is int
    assert request.schema_version == RENDER_SCHEMA_VERSION


def test_render_writes_universal_result_manifest(
    fake_service: _FakeService, tmp_path: Path
) -> None:
    timeline, assets, out = _inputs(tmp_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake-mp4")
    provenance = Path(f"{out}.provenance.json")
    provenance.write_text("{}\n", encoding="utf-8")
    fake_service.sentinel = out

    result = render_run.render(timeline, assets, out, selector="rendering.ffmpeg")

    assert result == out
    receipt = json.loads((out.parent / "manifest.json").read_text(encoding="utf-8"))
    assert receipt["kind"] == "rendering.render"
    assert [item["name"] for item in receipt["outputs"]] == ["video", "provenance"]
    assert receipt["outputs"][0]["path"] == out.name
    assert receipt["outputs"][0]["is_primary"] is True
    assert receipt["outputs"][1]["role"] == "auxiliary"


@pytest.mark.parametrize(
    "selector",
    ["rendering.remotion", "rendering.ffmpeg", "rendering.threejs"],
)
def test_render_forwards_qualified_selectors(
    fake_service: _FakeService, tmp_path: Path, selector: str
) -> None:
    timeline, assets, out = _inputs(tmp_path)

    render_run.render(timeline, assets, out, selector=selector)

    assert len(fake_service.calls) == 1
    assert fake_service.calls[0][1]["selector"] == selector


def test_render_maps_backend_kwargs_into_namespaced_backend_config(
    fake_service: _FakeService, tmp_path: Path
) -> None:
    timeline, assets, out = _inputs(tmp_path)

    render_run.render(
        timeline,
        assets,
        out,
        selector="rendering.remotion",
        project_dir=tmp_path / "remotion",
        composition_id="CustomComposition",
        theme_path=tmp_path / "theme.json",
        min_free_gb=2.0,
    )

    assert len(fake_service.calls) == 1
    config = fake_service.calls[0][0][0].backend_config
    assert config["rendering.remotion"] == {
        "project_dir": str(tmp_path / "remotion"),
        "composition_id": "CustomComposition",
        "theme_path": str(tmp_path / "theme.json"),
        "min_free_gb": 2.0,
    }


def test_render_passes_frame_window_to_shared_service(fake_service: _FakeService, tmp_path: Path) -> None:
    timeline, assets, out = _inputs(tmp_path)
    window = {"start_frame": 24, "end_frame": 72, "fps_rational": [24, 1]}
    fake_service.sentinel.write_bytes(b"fake-mp4")
    Path(f"{fake_service.sentinel}.provenance.json").write_text("{}\n", encoding="utf-8")

    render_run.render(timeline, assets, out, render_window=window)

    request = fake_service.calls[0][0][0]
    assert request.window.to_dict() == {**window, "source_range": None, "speed": None}


def test_render_merges_explicit_backend_config(
    fake_service: _FakeService, tmp_path: Path
) -> None:
    timeline, assets, out = _inputs(tmp_path)

    render_run.render(
        timeline,
        assets,
        out,
        selector="rendering.remotion",
        theme_path=tmp_path / "theme.json",
        backend_config={
            "rendering.remotion": {
                "theme_path": str(tmp_path / "override.json"),
                "min_free_gb": 9.5,
            }
        },
    )

    assert len(fake_service.calls) == 1
    config = fake_service.calls[0][0][0].backend_config
    assert config["rendering.remotion"]["theme_path"] == str(tmp_path / "override.json")
    assert config["rendering.remotion"]["min_free_gb"] == 9.5


def test_render_passes_previous_outputs_when_preserving(fake_service: _FakeService, tmp_path: Path) -> None:
    timeline, assets, out = _inputs(tmp_path)

    render_run.render(timeline, assets, out, keep_previous_renders=True)

    assert len(fake_service.calls) == 1
    assert fake_service.calls[0][1]["previous_outputs"] == ()


def test_render_validates_output_name_extension(tmp_path: Path) -> None:
    timeline, assets, _out = _inputs(tmp_path)
    bad_out = tmp_path / "out" / "video.mov"

    with pytest.raises(RendererProtocolError, match=r"\.mov|alpha"):
        render_run.render(timeline, assets, bad_out)


def test_main_accepts_output_name_and_forward_parses_any_order(
    fake_service: _FakeService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    timeline, assets, out = _inputs(tmp_path)
    out = tmp_path / "out" / "iteration.mp4"
    fake_service.sentinel = out

    result = render_run.main(
        [
            "--out",
            str(out),
            "--output-name",
            "iteration.mp4",
            "--timeline",
            str(timeline),
            "--assets",
            str(assets),
            "--selector",
            "rendering.ffmpeg",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out.strip() == str(out)
    assert len(fake_service.calls) == 1
    assert fake_service.calls[0][1]["selector"] == "rendering.ffmpeg"


def test_main_leaves_remotion_project_selection_to_deployment(
    fake_service: _FakeService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline, assets, out = _inputs(tmp_path)
    fake_service.sentinel = out
    monkeypatch.setenv(
        "ASTRID_REMOTION_PROJECT_DIR",
        str(tmp_path / "server-owned-remotion"),
    )

    result = render_run.main(
        [
            "--timeline",
            str(timeline),
            "--assets",
            str(assets),
            "--out",
            str(out),
        ]
    )

    assert result == 0
    request = fake_service.calls[-1][0][0]
    assert request.backend_config["rendering.remotion"] == {
        "composition_id": "TimelineComposition"
    }


def test_main_rejects_traversal_output_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRID_INTERNAL_INVOCATION", raising=False)
    timeline, assets, _out = _inputs(tmp_path)

    result = render_run.main(
        [
            "--timeline",
            str(timeline),
            "--assets",
            str(assets),
            "--out",
            str(tmp_path / "out" / "hype.mp4"),
            "--output-name",
            "../evil.mp4",
        ]
    )

    assert result == 1
    assert "traverse" in capsys.readouterr().err


def test_main_surfaces_bounded_structured_renderer_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timeline, assets, out = _inputs(tmp_path)

    class UnsupportedService:
        def render(self, *args, **kwargs):
            raise_unsupported_error(
                backend="rendering.remotion",
                message="Remotion does not support this render request",
                details={"reasons": ["timeline clip label is not admitted", "x" * 5_000]},
            )

    monkeypatch.setattr(render_run, "_default_service", lambda: UnsupportedService())
    result = render_run.main(
        [
            "--timeline",
            str(timeline),
            "--assets",
            str(assets),
            "--out",
            str(out),
        ]
    )

    stderr = capsys.readouterr().err
    assert result == 1
    assert "timeline clip label is not admitted" in stderr
    assert "renderer detail truncated" in stderr
    assert len(stderr) <= 3_501


def test_main_selector_defaults_to_remotion_when_absent(
    tmp_path: Path, fake_service: _FakeService
) -> None:
    timeline, assets, out = _inputs(tmp_path)
    fake_service.sentinel = out

    result = render_run.main(
        [
            "--timeline",
            str(timeline),
            "--assets",
            str(assets),
            "--out",
            str(out),
        ]
    )

    assert result == 0
    assert len(fake_service.calls) == 1
    assert fake_service.calls[0][1]["selector"] == "rendering.remotion"


@pytest.mark.parametrize('review', [False, True])
def test_review_metadata_is_render_only(fake_service, tmp_path, review):
    timeline, assets, out = _inputs(tmp_path)
    before = timeline.read_bytes(), assets.read_bytes()
    context = {'shots': [{'shot_id': 'one', 'name': '01 Opening', 'at': 0, 'hold': 2}]}
    render_run.render(timeline, assets, out, review=review, review_context=context)
    request = fake_service.calls[0][0][0]
    import json
    assert (json.loads(request.metadata['review']) if review else request.metadata) == (context if review else {})
    assert (timeline.read_bytes(), assets.read_bytes()) == before


def test_ffmpeg_review_rejects_in_support_before_render(tmp_path):
    import json
    from astrid.packs.rendering.rendering.renderers.ffmpeg.run import support
    request = RenderRequest.from_dict({'schema_version': 1, 'timeline_path': str(tmp_path / 'timeline.json'), 'output_name': 'review.mp4', 'metadata': {'review': json.dumps({'shots': []})}})
    report = support(request, workspace=tmp_path)
    assert not report.supported
    assert 'Review overlay requires' in report.reasons[0]


def test_remotion_lock_handoff_keeps_review_context(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from astrid.packs.rendering.rendering.renderers.remotion import run as backend
    captured = {}
    monkeypatch.setattr(backend.remotion_lock, 'remotion_render_lock', lambda: nullcontext())
    def locked(*args, **kwargs):
        captured.update(kwargs)
        return 'rendered'
    monkeypatch.setattr(backend, '_execute_remotion_locked', locked)
    context = {'shots': [{'shot_id': 'a', 'name': 'Opening', 'at': 0, 'hold': 2}]}
    assert backend._execute_remotion(tmp_path / 'timeline.json', tmp_path / 'assets.json', tmp_path / 'staged.mp4', provenance_out_path=tmp_path / 'output.mp4', project_dir=tmp_path, composition_id='TimelineComposition', theme_path=None, min_free_gb=None, review=context) == 'rendered'
    assert captured['review'] == context
