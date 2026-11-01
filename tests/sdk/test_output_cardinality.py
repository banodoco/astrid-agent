"""Phase-1 output cardinality: exactly one declared sink, counted as VibeComfy counts them."""

import json

from astrid.sdk.generation import _declared_output_count


def _companion(tmp_path, document):
    path = tmp_path / "workflow.vibe.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_single_declared_sink_counts_one(tmp_path):
    path = _companion(tmp_path, {"outputs": [{"node_id": "9", "output_type": "SaveImage"}]})
    assert _declared_output_count(path) == 1


def test_two_declared_sinks_count_two(tmp_path):
    path = _companion(tmp_path, {"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": "10", "output_type": "SaveVideo"},
    ]})
    assert _declared_output_count(path) == 2


def test_mixed_role_entries_without_node_id_do_not_count(tmp_path):
    path = _companion(tmp_path, {"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": None, "output_type": "PreviewImage"},
    ]})
    assert _declared_output_count(path) == 1


def test_no_declared_outputs_counts_zero(tmp_path):
    path = _companion(tmp_path, {"outputs": []})
    assert _declared_output_count(path) == 0


def test_undeterminable_companion_returns_none(tmp_path):
    assert _declared_output_count(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert _declared_output_count(bad) is None
    assert _declared_output_count(_companion(tmp_path, {"nodes": {}})) is None


class _FakeTasks:
    def __init__(self):
        self.admitted = []

    def prepare_admission(self, *args, **kwargs):
        self.admitted.append(("prepare", args, kwargs))
        raise AssertionError("no task may be prepared for a refused workflow")

    def dispatch_admission(self, *args, **kwargs):
        self.admitted.append(("dispatch", args, kwargs))
        raise AssertionError("no task may be dispatched for a refused workflow")


class _FakeRuntime:
    def __init__(self):
        self.tasks = _FakeTasks()


def _refuse(companion_doc, tmp_path):
    from astrid.sdk.generation import GenerationFacade

    runtime = _FakeRuntime()
    workflow = tmp_path / "workflow.py"
    workflow.write_text("# placeholder source\n", encoding="utf-8")
    try:
        GenerationFacade().workflow(workflow=workflow, companion=companion_doc, client=runtime, dry_run=True)
    except Exception as exc:  # noqa: BLE001 - the assertion below inspects the message
        return str(exc), runtime
    raise AssertionError("a workflow with other than one declared output was not refused")


def test_two_output_companion_is_refused_before_admission(tmp_path):
    message, runtime = _refuse({"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": "10", "output_type": "SaveVideo"},
    ]}, tmp_path)
    assert "produces 2 outputs; Phase 1 supports exactly one" in message
    assert runtime.tasks.admitted == []


def test_mixed_role_companion_is_refused_before_admission(tmp_path):
    message, runtime = _refuse({"outputs": [
        {"node_id": "9", "output_type": "SaveImage"},
        {"node_id": "11", "output_type": "SaveVideo"},
        {"node_id": None, "output_type": "PreviewImage"},
    ]}, tmp_path)
    assert "produces 2 outputs; Phase 1 supports exactly one" in message
    assert runtime.tasks.admitted == []


def test_single_declared_output_passes_the_cardinality_check(tmp_path):
    from astrid.sdk.generation import GenerationFacade

    workflow = tmp_path / "workflow.py"
    workflow.write_text("# placeholder source\n", encoding="utf-8")
    try:
        GenerationFacade().workflow(workflow=workflow, companion={"outputs": [
            {"node_id": "9", "output_type": "SaveImage"}]}, client=_FakeRuntime(), dry_run=True)
    except Exception as exc:  # noqa: BLE001 - only the cardinality refusal is forbidden here
        assert "Phase 1 supports exactly one" not in str(exc)


def test_companion_less_workflow_is_not_refused_by_the_cardinality_check(tmp_path):
    from astrid.sdk.generation import GenerationFacade

    workflow = tmp_path / "workflow.py"
    workflow.write_text("# plain source, no sealed companion\n", encoding="utf-8")
    try:
        GenerationFacade().workflow(workflow=workflow, client=_FakeRuntime(), dry_run=True)
    except Exception as exc:  # noqa: BLE001 - only the cardinality refusal is forbidden here
        assert "Phase 1 supports exactly one" not in str(exc)
