from pathlib import Path

import yaml


PACK = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "rendering"


def test_render_action_has_no_optional_pipeline_prerequisites_or_builder() -> None:
    pack = yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))
    render = pack["actions"]["render"]

    assert not render["graph"].get("depends_on")
    assert render["graph"]["provides"] == ["rendered_video"]
    assert "pipeline_requirements" not in render
    assert not {
        "command_builder",
        "pipeline_step",
        "pipeline_step_order",
    } & render["metadata"].keys()
    assert render["invocation"]["kind"] == "command"
