"""Construction checks for diagnostic M4-6 names, not provenance proof."""

from pathlib import Path

from scripts.journey_matrix import M4_6_MATRIX


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml"
JOB_NAME = "m4-6-local-${{ matrix.os }}-${{ matrix.job_id }}"
ARTIFACT_NAME = (
    "m4-6-local-${{ github.run_id }}-${{ github.run_attempt }}-"
    "${{ matrix.os }}-${{ matrix.job_id }}"
)
CURRENT_ATTEMPT_PATTERN = "m4-6-local-${{ github.run_id }}-${{ github.run_attempt }}-*"


def _workflow():
    return WORKFLOW.read_text(encoding="utf-8")


def _render(template, *, run, attempt, slot, os_name):
    return (template.replace("${{ github.run_id }}", str(run))
            .replace("${{ github.run_attempt }}", str(attempt))
            .replace("${{ matrix.job_id }}", slot)
            .replace("${{ matrix.os }}", os_name))


def test_exact_workflow_names_and_attempt_scoped_download():
    workflow = _workflow()
    job = workflow.split("\n  failure_modes:\n", 1)[1].split("\n  gate:\n", 1)[0]
    assert f"    name: {JOB_NAME}\n" in job
    assert job.count("uses: actions/upload-artifact@v4") == 1
    assert f"          name: {ARTIFACT_NAME}\n" in job
    assert f"          pattern: {CURRENT_ATTEMPT_PATTERN}\n" in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert '"m4_6_matrix", {"include": list(journey_matrix.M4_6_MATRIX)}' in workflow
    assert "matrix: ${{ fromJSON(needs.config.outputs.m4_6_matrix) }}" in job
    assert "Fail closed on the unavailable hosted M4-6 native-service aggregate" in workflow


def test_names_distinguish_slots_runs_and_attempts():
    slots = [row["job_id"] for row in M4_6_MATRIX]
    assert len(slots) == len(set(slots)) == 9
    jobs = {
        _render(JOB_NAME, run=100, attempt=1, slot=row["job_id"], os_name=row["os"])
        for row in M4_6_MATRIX
    }
    names = {
        _render(ARTIFACT_NAME, run=run, attempt=attempt,
                slot=row["job_id"], os_name=row["os"])
        for run in (100, 101) for attempt in (1, 2) for row in M4_6_MATRIX
    }
    assert len(jobs) == 9
    assert len(names) == 36
    assert _render(ARTIFACT_NAME, run=100, attempt=1, slot="direct-linux", os_name="linux") == (
        "m4-6-local-100-1-linux-direct-linux"
    )
