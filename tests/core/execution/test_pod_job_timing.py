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
