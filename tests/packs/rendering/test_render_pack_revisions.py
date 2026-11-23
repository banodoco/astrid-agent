import json
from contextlib import contextmanager
from types import SimpleNamespace

from astrid.core.pack.identity import pack_digest, write_pin
from astrid.packs.rendering.rendering.renderers.remotion.pack_revisions import resolved_pack_revisions


def test_selected_pack_reports_upstream_and_detects_changed_bytes(tmp_path):
    root = tmp_path / "astrid_motion"
    element = root / "rendering/elements/effects/am-type"
    element.mkdir(parents=True)
    (root / "pack.yaml").write_text("id: astrid_motion\n")
    (root / "render-provenance.json").write_text(json.dumps({"derived_from": "a" * 40}))
    (element / "component.tsx").write_text("original")
    write_pin(root / "PIN", {"version": 1, "source": "https://example.com/repo",
                            "commit": "b" * 40, "digest_rule": "pack-tree-v1",
                            "digest": pack_digest(root), "origin": "user"})
    stage = {"effects": [{"source_pack_id": "astrid_motion", "element_root": str(element)}]}
    row = resolved_pack_revisions(stage, {"astrid_motion": "c" * 40})[0]
    assert (row["resolved_revision"], row["derived_from"], row["editor_revision"], row["comparison"]) == (
        "b" * 40, "a" * 40, "c" * 40, "mismatch")
    assert resolved_pack_revisions(stage, {"astrid_motion": "a" * 40})[0]["comparison"] == "match"
    assert resolved_pack_revisions(stage, None)[0]["comparison"] == "editor revision not supplied"
    (element / "component.tsx").write_text("modified")
    row = resolved_pack_revisions(stage, {"astrid_motion": "a" * 40})[0]
    assert row["resolved_revision"] is None
    assert row["comparison"] == "resolved upstream revision unavailable"


def test_mismatch_is_reported_before_renderer_launch(tmp_path, monkeypatch, capsys):
    from astrid.packs.rendering.rendering.renderers.remotion import run

    row = {"pack_id": "astrid_motion", "editor_revision": "a" * 40,
           "derived_from": "b" * 40, "resolved_revision": "c" * 40,
           "comparison": "mismatch"}
    stage = {}
    output = tmp_path / "stage.mov"

    @contextmanager
    def prepared(*args, **kwargs):
        yield ({"timeline": {"clips": []}}, tmp_path / "props.json",
               SimpleNamespace(node_executable="node", remotion_cli="cli", node_version="test"),
               {}, {}, stage, tmp_path, 1234, "hash", tmp_path / "public", None,
               True, None, False, output)

    def launch(*args, **kwargs):
        warning = capsys.readouterr().err
        assert row["editor_revision"] in warning
        assert row["derived_from"] in warning
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run, "_prepared_remotion_root", prepared)
    monkeypatch.setattr(run, "resolved_pack_revisions", lambda summary, editor: [row])
    monkeypatch.setattr(run.subprocess, "run", launch)
    details = run._execute_remotion_locked(
        tmp_path / "timeline.json", tmp_path / "assets.json", output,
        provenance_out_path=tmp_path / "final.mov", project_dir=tmp_path,
        composition_id="TimelineComposition", theme_path=None, min_free_gb=None,
        editor_pack_revisions={"astrid_motion": "a" * 40})
    assert details.stage_summary["pack_revisions"] == [row]
