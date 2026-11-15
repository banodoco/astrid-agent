"""Pod timing is local evidence, ingested without another remote operation."""
import json

from astrid.core.execution import phase_timing


def test_v1_ingests_the_pod_span_without_replacing_controller_phases(tmp_path):
    path = tmp_path / "job-run.json"
    record = {"schema": "astrid.job-run.v1", "phases": {"whole_job": 15.0, "unaccounted": 7.0,
                                                        "execute": 4.0}, "notes": ["private note"]}
    path.write_text(json.dumps(record))
    timing = phase_timing.PhaseTiming(clock=lambda: 10)
    timing.stamp("job_started_at")
    timing.stamp("job_ended_at", at=30)
    phase_timing.ingest_job_run(timing, {"job-run.json": str(path)})
    block = timing.block()
    assert block["phases"]["execution"]["seconds"] == 20.0
    assert block["pod_job"] == {"schema": record["schema"], "phases": record["phases"]}
    assert "private note" not in json.dumps(block)


def test_v1_ingests_collected_run_log_and_ignores_bad_optional_evidence(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"schema": "astrid.job-run.v1", "phases": {"whole_job": 4, "unaccounted": None}}))
    timing = phase_timing.PhaseTiming()
    phase_timing.ingest_job_run(timing, {"logs/run.json": str(path)})
    assert timing.block()["pod_job"]["phases"] == {"whole_job": 4, "unaccounted": None}
    path.write_text("bad JSON")
    phase_timing.ingest_job_run(timing, {"job-run.json": str(path)})
    assert timing.block()["pod_job"]["phases"]["whole_job"] == 4
    phase_timing.ingest_job_run(None, {})


def test_v2_nests_whole_pod_span_without_double_counting(tmp_path):
    record = {'schema': 'astrid.job-run.v1', 'phases': {
        'whole_job': 15, 'unaccounted': 7, 'execute': 8}, 'engine_reused': True}
    (tmp_path / 'job-run.json').write_text(json.dumps(record))
    clock = [10.0]
    timing = phase_timing.GenerationTiming(clock=lambda: clock[0])
    timing.transition('execution')
    clock[0] = 30
    timing.read_job(tmp_path)
    block = timing.block()
    execution = block['phases']['execution']
    assert block['tier1_cards'] == list(phase_timing.TIER1_CARDS)
    assert block['tier1_cards'] == ["P3", "P4", "W-C", "W-D", "W-E", "W-F", "W-H", "Q2"]
    assert execution['seconds'] == 20
    assert execution['pod_job'] == {'whole_job': {'seconds': 15.0}, 'unaccounted': {'seconds': 7.0}}
    assert execution['sub_phases_seconds'] == 8
    assert execution['unaccounted']['raw_seconds'] == 12


def test_named_pod_steps_have_exclusive_budget_and_keep_nested_wall_time():
    clock = [10.0]
    timing = phase_timing.PhaseTiming(clock=lambda: clock[0])
    with phase_timing.pod_timing_scope(timing):
        with phase_timing.pod_phase('preparation', 'prepare'):
            clock[0] += 2
            with phase_timing.pod_phase('doctor', 'doctor'):
                clock[0] += 3
                with phase_timing.pod_phase('engine_health', 'doctor-health'):
                    clock[0] += 5
            clock[0] += 7
    rows = timing.named_steps()
    assert [(r['phase'], r['step_id'], r['seconds'], r['exclusive_seconds']) for r in rows] == [
        ('preparation', 'prepare', 17, 9), ('doctor', 'doctor', 8, 3),
        ('engine_health', 'doctor-health', 5, 5)]
    assert sum(r['exclusive_seconds'] for r in rows) == 17
    assert sum(r['seconds'] for r in rows if r['parent'] is None) == 17


def test_named_steps_survive_v1_and_v2_ingestion_without_overlap(tmp_path):
    record = {'schema': 'astrid.job-run.v1', 'phases': {
        'whole_job': 20, 'unaccounted': 0, 'verify_models': 5,
        'engine_start_or_reuse': 3, 'execute': 4},
        'top_level_phases': {
            'prepare': {'phase': 'preparation', 'step_id': 'prepare', 'seconds': 16},
            'execute': {'phase': 'execute', 'step_id': 'execute', 'seconds': 4}},
        'named_phases': [
            {'phase': 'preparation', 'step_id': 'prepare', 'seconds': 16, 'parent': None},
            {'phase': 'verify_models', 'step_id': 'verify-models', 'seconds': 5, 'parent': 0}]}
    path = tmp_path / 'job-run.json'
    path.write_text(json.dumps(record))
    timing = phase_timing.PhaseTiming()
    phase_timing.ingest_job_run(timing, {'job-run.json': path})
    assert timing.block()['pod_job']['top_level_phases']['prepare']['seconds'] == 16
    assert timing.block()['pod_job']['named_phases'][1]['step_id'] == 'verify-models'
    clock = [0.0]
    host = phase_timing.GenerationTiming(clock=lambda: clock[0])
    host.transition('execution')
    clock[0] = 20
    host.read_job(tmp_path)
    execution = host.block()['phases']['execution']
    assert execution['sub_phases_seconds'] == 20
    assert execution['unaccounted']['raw_seconds'] == 0
    assert execution['pod_job']['named_phases'][1]['seconds'] == 5


def test_collection_status_reports_pod_time_without_another_operation(tmp_path, monkeypatch, capsys):
    import time
    from types import SimpleNamespace

    from astrid.core.generation import engine_runner

    monkeypatch.setenv('ASTRID_POD_PHASE_TIMING', '1')
    operations = []

    def fact(*args, **kwargs):
        operations.append('receipt-stat')
        return SimpleNamespace(exists=False, symlink=False)

    context = SimpleNamespace(platform='linux-x86_64-cu130', placement='local', file_fact=fact)
    result = engine_runner.engine_status('comfyui', platform=context.platform, placement='local',
        root=str(tmp_path / 'engine'), context=context, deadline=time.monotonic() + 30)
    assert result is None
    assert operations == ['receipt-stat']  # Timing adds no engine/transport operation.
    reports = phase_timing.read_pod_reports(capsys.readouterr().err)
    assert reports[0]['named_phases'][0]['phase'] == 'collection_engine_status'
    assert reports[0]['named_phases'][0]['step_id'] == 'engine-status'
    assert reports[0]['named_phases'][0]['seconds'] >= 0
    host = phase_timing.GenerationTiming()
    controller = phase_timing.PhaseTiming(generation=host)
    controller.pod_job = {'phases': {'whole_job': 12}}
    phase_timing.attach_pod_recheck(controller, reports)
    assert controller.pod_job['phases']['whole_job'] == 12
    assert controller.pod_job['collection_recheck'] == reports
    assert host.block()['phases']['collection']['pod_recheck'] == reports


def test_optional_clock_failure_preserves_the_operation_result_and_error():
    import pytest

    def unavailable():
        raise OSError('clock unavailable')

    @phase_timing.pod_step('doctor', 'doctor')
    def operation(result):
        if isinstance(result, Exception):
            raise result
        return result

    failure = ValueError('original doctor error')
    with phase_timing.pod_timing_scope(phase_timing.PhaseTiming(clock=unavailable)):
        assert operation('unchanged result') == 'unchanged result'
        with pytest.raises(ValueError) as caught:
            operation(failure)
        assert caught.value is failure


def test_timing_decorators_remain_in_the_verified_helper_code_closure():
    import hashlib

    from astrid.core.generation import engine_recipe, engine_runner

    recipe = engine_recipe.load_recipe('comfyui', 'linux-x86_64-cu130')
    manifest = engine_runner.helper_manifest(recipe)
    path = 'core/execution/phase_timing.py'
    assert manifest[path] == hashlib.sha256((engine_recipe._ROOT / path).read_bytes()).hexdigest()
