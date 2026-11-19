"""LS-12: the caller transfers the canonical pair it already emitted."""
import json

from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade
from tests.sdk.test_generation_source_retention import admission  # noqa: F401


def test_named_workflow_emits_once_and_transfers_the_bound_pair(monkeypatch, admission, tmp_path):
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy import workflow_bundle as bundles

    workflow = VibeWorkflow("ls12-bound", WorkflowSource("ls12-bound"))
    workflow.add_node("SaveImage", _id="1", uid="save", filename_prefix="original")
    workflow.add_node("CLIPTextEncode", _id="12", uid="prompt", text="original")
    workflow.register_input("prompt", "12", "text", "original", type="STRING")
    source = bundles.emit_bundle(workflow, tmp_path / "workflow.py", {"operation": "authored"})
    emitted = []
    original = bundles.emit_bundle

    def observe(*args, **kwargs):
        bundle = original(*args, **kwargs)
        emitted.append((bundle.python_path.read_bytes(), bundle.python_path.with_suffix(".vibe.json").read_bytes()))
        return bundle

    monkeypatch.setattr(bundles, "emit_bundle", observe)
    result = GenerationFacade()._generate_on_target(
        registry=ModelRegistry([]), model=None, mode="t2i", modality="image",
        placement="managed", execution=None, inputs={"prompt": "requested"}, dry_run=False,
        workflow_entry={"id": "ls12-bound", "kind": "image", "modes": ["t2i"],
                        "bundle": str(source.python_path), "binds": {}, "defaults": {}, "needs": []},
        client=admission.client, project="demo", wait=False,
    )
    assert result is admission.task
    supplied = admission.invoke.call_args.kwargs
    pair = tuple(admission.stored[supplied[name]["digest"]] for name in ("python", "companion"))
    assert len(emitted) == 1
    assert pair == emitted[0]
    assert json.loads(supplied["workflow_inputs"])["prompt"] == "requested"
    copied = tmp_path / "readback"
    copied.mkdir()
    (copied / "workflow.py").write_bytes(pair[0])
    (copied / "workflow.vibe.json").write_bytes(pair[1])
    reopened = bundles.load_bundle(copied / "workflow.py", trust=Provenance.USER_CONFIRMED)
    assert reopened.workflow.nodes["12"].inputs["text"] == "requested"
    assert all(not path.exists() for path in admission.paths)
