import ast
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


def test_rendering_has_no_understanding_import_or_scorer_resource() -> None:
    pack = yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8"))
    understanding_pack = yaml.safe_load(
        (PACK.parent / "understanding" / "pack.yaml").read_text(encoding="utf-8")
    )
    imported_modules = set()
    for source in PACK.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert not any(module.startswith("astrid.packs.understanding") for module in imported_modules)
    assert not any(
        resource.get("path") == "actions/timeline_visualize/scorer.py"
        for action in pack["actions"].values()
        for resource in action.get("resources", [])
    )
    assert {
        resource["path"]
        for resource in understanding_pack["actions"]["visual_understand"]["resources"]
    } >= {"actions/visual_understand/scorer.py"}
