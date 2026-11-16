"""visualize --out DIR exports the settled view; it used to be refused outright."""
from pathlib import Path
import json
import sys
import shutil
from types import SimpleNamespace

import pytest

from astrid.packs.timeline.cli import _export_visualization, _visualization_navigation_help
from astrid.packs.timeline.cli_visualize import _visualization_summary


def test_out_copies_pages_and_raw_frames(tmp_path: Path) -> None:
    view = tmp_path / "cache" / "abc"
    (view / "frames").mkdir(parents=True)
    page = view / "view-frame-94.60-94.63.png"
    page.write_bytes(b"page")
    markdown = view / "view.md"
    markdown.write_text("# Inputs\n")
    (view / "frames" / "frame-000002838.png").write_bytes(b"frame")
    (view / "frame-index.json").write_text("{}")
    out = tmp_path / "export"

    exported = _export_visualization(
        {"primary_page": str(page), "pages": [str(page)], "markdown": str(markdown)}, str(out)
    )

    assert exported["pages"] == [str(out / page.name)]
    assert exported["markdown"] == str(out / markdown.name)
    assert (out / markdown.name).read_text() == "# Inputs\n"
    assert exported["frames"] == [str(out / "frames" / "frame-000002838.png")]
    assert (out / "frame-index.json").is_file()
    assert page.is_file()  # the managed copy stays


def test_out_materializes_managed_markdown_artifact(tmp_path: Path) -> None:
    out = tmp_path / "artifacts"
    calls = []

    class Media:
        def open(self, project, object_id, **kwargs):
            calls.append((project, object_id, kwargs))
            filename, payload = (
                ("view.png", b"Runtime PNG") if object_id == "sha256:png"
                else ("inputs.md", b"# Runtime artifact\n")
            )
            path = Path(kwargs["cache_root"]) / filename
            path.write_bytes(payload)
            return SimpleNamespace(ok=True, data={"local_path": str(path)})

    client = SimpleNamespace(media=Media())
    exported = _export_visualization(
        {"artifacts": {
            "png": {"object_id": "sha256:png", "filename": "view.png"},
            "md": {"object_id": "sha256:abc", "filename": "inputs.md"},
        }},
        str(out), client=client, project="minkhole",
    )

    assert [call[:2] for call in calls] == [
        ("minkhole", "sha256:png"), ("minkhole", "sha256:abc"),
    ]
    assert all(call[2]["materialize"] is True for call in calls)
    assert all(call[2]["max_bytes"] == 64 * 1024 * 1024 for call in calls)
    assert Path(exported["pages"][0]).read_bytes() == b"Runtime PNG"
    assert exported["markdown"] == str(out / "inputs.md")
    assert (out / "inputs.md").read_text() == "# Runtime artifact\n"


def test_media_materialization_failure_is_typed_without_success_summary(tmp_path, capsys):
    from astrid.packs.timeline import cli
    from astrid.sdk.contracts import DomainResult

    class Timelines:
        def open_bundle(self, project, ref, revision_id=None):
            return SimpleNamespace(ok=True, data={"bundle": {"base_parent": {"revision_id": "rev-1"}}})

        def visualize(self, *args, **kwargs):
            return DomainResult.success({"artifacts": {
                "png": {"object_id": "sha256:managed-png", "filename": "view.png"},
            }})

    class Media:
        def open(self, project, object_id, **kwargs):
            return SimpleNamespace(ok=False, error={"code": "size_limit", "message": "artifact exceeds export limit"})

    client = SimpleNamespace(timelines=Timelines(), media=Media())
    parser = cli.build_parser(client)
    parsed = parser.parse_args(["visualize", "main", "--project", "demo", "--mode", "inputs",
                                "--out", str(tmp_path / "out"), "--json"])

    assert cli._cmd_visualize(parsed) == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "size_limit"
    assert envelope["error"]["message"] == "artifact exceeds export limit"
    assert envelope["error"]["details"]["artifacts"]["png"]["object_id"] == "sha256:managed-png"
    assert envelope["error"]["details"]["files_written"] == []
    assert "export directory:" not in captured.out
    assert "filmstrip:" not in captured.out
    assert "unstructured" not in captured.err

    parsed = parser.parse_args(["visualize", "main", "--project", "demo", "--mode", "inputs",
                                "--out", str(tmp_path / "out-human")])
    assert cli._cmd_visualize(parsed) == 1
    human = capsys.readouterr()
    assert human.out == ""
    assert "size_limit: artifact exceeds export limit" in human.err
    assert "unstructured" not in human.err


def test_export_write_failure_keeps_managed_references_and_partial_file_list(tmp_path, monkeypatch):
    from astrid.packs.timeline.cli import VisualizationExportError
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    monkeypatch.setattr(shutil, "copy2", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(VisualizationExportError, match="disk full") as failure:
        _export_visualization(
            {"pages": [str(source)], "artifacts": {"png": {
                "status": "unavailable", "object_id": "sha256:managed",
            }}},
            str(tmp_path / "export"),
        )

    assert failure.value.code == "invocation_error"
    assert failure.value.artifacts["png"]["object_id"] == "sha256:managed"
    assert failure.value.written == []


def test_navigation_suggestions_preserve_python_module_invocation(monkeypatch) -> None:
    monkeypatch.setattr(sys, "orig_argv", ["/venv/bin/python", "-m", "astrid", "timelines", "visualize"], raising=False)
    monkeypatch.setattr(sys, "argv", ["/checkout/astrid/__main__.py", "timelines", "visualize"])
    navigation = _visualization_navigation_help(
        project="minkhole",
        inputs={"timeline_slug": "rough-cut", "view": "filmstrip"},
        outputs={"pages": ["/tmp/view.png"], "manifest_path": "/tmp/manifest.json"},
    )

    commands = navigation["commands"]
    assert commands["rerun"].startswith("/venv/bin/python -m astrid timelines visualize ")
    assert commands["show"].startswith("/venv/bin/python -m astrid timelines show ")
    assert commands["inspect_summary"].startswith("/venv/bin/python -m astrid timelines inspect ")
    assert commands["controls"] == "/venv/bin/python -m astrid timelines visualize --help"


def test_navigation_suggestions_preserve_console_launcher(monkeypatch) -> None:
    monkeypatch.setattr(sys, "orig_argv", ["/venv/bin/python", "/opt/astrid/bin/astrid", "timelines"], raising=False)
    monkeypatch.setattr(sys, "argv", ["/opt/astrid/bin/astrid", "timelines", "visualize"])
    navigation = _visualization_navigation_help(
        project="minkhole",
        inputs={"timeline_slug": "rough-cut", "view": "filmstrip"},
        outputs={"pages": ["/tmp/view.png"], "manifest_path": "/tmp/manifest.json"},
    )

    assert navigation["commands"]["rerun"].startswith("/opt/astrid/bin/astrid timelines visualize ")
    assert navigation["commands"]["controls"] == "/opt/astrid/bin/astrid timelines visualize --help"


def test_render_export_suggestion_uses_media_open_materialization_flags():
    from astrid.packs.timeline.cli import _render_video_handle

    result = SimpleNamespace(
        kernel_run_id="run-123", run_id=None,
        outputs={"managed_outputs": [{"output_port": "video", "ordinal": 0}]},
    )
    suggestion = _render_video_handle(result, "demo")["export"]
    assert suggestion == (
        "media open --project demo 'run:run-123/video#0' --materialize --cache-root ./rendered-video"
    )


def test_declared_inputs_summary_explains_no_picture_and_suggests_composed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "orig_argv", ["/venv/bin/python", "-m", "astrid", "timelines"], raising=False)
    monkeypatch.setattr(sys, "argv", ["/checkout/astrid/__main__.py", "timelines"])
    summary = _visualization_summary(
        {
            "evidence_kind": "declared_inputs",
            "artifacts": {"md": {"filename": "timeline-inputs.md", "object_id": "sha256:abc"}},
            "exported": {"markdown": "/tmp/artifacts/timeline-inputs.md", "pages": []},
        },
        parsed=SimpleNamespace(project="minkhole", beats=None),
        inputs={"view": "filmstrip", "timeline_slug": "rough-cut", "at": "6.5"},
    )

    assert "no picture produced (inputs mode is render-free" in summary
    assert "declared inputs inspected; no frames captured" in summary
    assert "None frames" not in summary
    assert "Markdown artifact: timeline-inputs.md" in summary
    assert "exported Markdown: /tmp/artifacts/timeline-inputs.md" in summary
    assert "/venv/bin/python -m astrid timelines visualize rough-cut --project minkhole --mode composed --format png --at 6.5" in summary
    assert "--mode inputs --format md --out artifacts" in summary


def test_single_composed_export_names_the_clean_raw_frame_path() -> None:
    summary = _visualization_summary(
        {
            "primary_page": "/tmp/export/view-frame.png",
            "pages": ["/tmp/export/view-frame.png"],
            "exported": {
                "dir": "/tmp/export",
                "pages": ["/tmp/export/view-frame.png"],
                "frames": ["/tmp/export/frames/frame-000000156.png"],
            },
        },
        parsed=SimpleNamespace(project="minkhole", beats=None, mode="composed"),
        inputs={"view": "filmstrip", "timeline_slug": "rough-cut", "at": "6.5"},
    )

    assert "clean composed frame (no visualization overlays): /tmp/export/frames/frame-000000156.png" in summary


def test_single_export_frame_is_described_neutrally_when_composition_is_uncertain() -> None:
    for mode, overlays in (("auto", {}), ("composed", {"layers": ["bounds"]}),
                           ("composed", {"highlight": "c41.tool-16"})):
        summary = _visualization_summary(
            {"exported": {"frames": ["/tmp/export/frames/frame-000000156.png"]}},
            parsed=SimpleNamespace(project="minkhole", beats=None, mode=mode),
            inputs={"view": "filmstrip", "at": "6.5", **overlays},
        )
        assert "raw frame image: /tmp/export/frames/frame-000000156.png" in summary
        assert "clean composed frame" not in summary


def test_composed_summary_uses_the_pinned_revision_from_source_identity() -> None:
    summary = _visualization_summary(
        {"source_identity": {"kind": "revision", "revision_id": "rev-pinned"}},
        parsed=SimpleNamespace(project="demo", beats=None, mode="composed"),
        inputs={"view": "filmstrip", "timeline_slug": "main"},
    )
    assert "  committed revision rev-pinned" in summary.splitlines()
    assert "revision unavailable" not in summary
