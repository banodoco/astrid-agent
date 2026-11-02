from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from astrid.packs.wan2gp.src.compiler import (
    compile_from_inputs,
    portable_digest,
    resident_key,
    runner_fingerprint,
    warmth_identity,
)
from astrid.packs.wan2gp.src.driver import (
    CancellationPolicy,
    CancellationToken,
    FakePersistentRunner,
    PersistentRunnerState,
    _canonicalize_generated_files,
)


@pytest.fixture
def settings() -> dict[str, object]:
    return compile_from_inputs(
        {
            "prompt": "a paper kite over a quiet lake",
            "model": "wan-2.2",
            "resolution": "320x240",
            "frames": 3,
            "fps": 12,
            "seed": 17,
            "wan2gp_path": "/machine/checkout-a",
            "attempt_root": "/machine/attempt-a",
        }
    )


def test_cancellation_token_is_cooperative_and_idempotent() -> None:
    token = CancellationToken()
    assert token.cancelled is False
    assert token.cancel("fixture stop") is True
    assert token.cancel("ignored second stop") is False
    assert token.cancelled is True
    assert token.reason == "fixture stop"
    with pytest.raises(RuntimeError, match="fixture stop"):
        token.raise_if_cancelled()


def test_fake_cancellation_leaves_persistent_runner_warm(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    state_path = tmp_path / "runner-state.jsonl"
    runner = FakePersistentRunner(state_path, output_root=tmp_path / "outputs")

    result = runner.run(
        settings,
        policy=CancellationPolicy(cancel_after_steps=1),
        work_steps=3,
    )

    assert result.status == "cancelled"
    assert result.cancelled is True
    assert result.generated_files == []
    assert result.runner_alive is True
    assert result.state.status == PersistentRunnerState.WARM
    events = [json.loads(line) for line in state_path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "runner_created",
        "runner_started",
        "run_started",
        "run_cancelled",
    ]
    assert events[-1]["reason"] == "cancelled by policy at step 1"


def test_persistent_state_reopens_and_reuses_warm_identity(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    state_path = tmp_path / "runner-state.jsonl"
    output_root = tmp_path / "outputs"
    first = FakePersistentRunner(state_path, output_root=output_root, runner_id="fixture-runner")
    first_result = first.run(settings, fixture={"fixture": "one"})
    assert first_result.status == "succeeded"
    assert first.is_alive() is True

    reopened_state = PersistentRunnerState.load(state_path)
    assert reopened_state.snapshot.status == PersistentRunnerState.WARM
    assert reopened_state.is_alive() is True
    second = FakePersistentRunner(
        state_path,
        output_root=output_root,
        runner_id="fixture-runner",
    )
    second_result = second.run(settings, output_name="second.json", fixture={"fixture": "two"})
    assert second_result.status == "succeeded"
    assert second_result.fingerprint == first_result.fingerprint
    assert second_result.warmth_identity == first_result.warmth_identity
    assert second_result.state.total_runs == 2
    assert second_result.state.successful_runs == 2
    assert second_result.state.status == PersistentRunnerState.WARM
    assert json.loads((output_root / "second.json").read_text()) == {"fixture": "two"}


def test_runner_and_warmth_identity_are_portable_and_deterministic(
    settings: dict[str, object],
) -> None:
    relocated = dict(settings)
    relocated.update(
        {
            "wan2gp_path": "/another/machine/checkout",
            "attempt_root": "/another/machine/attempt",
            "device": "cuda:7",
        }
    )
    assert portable_digest(settings) == portable_digest(relocated)
    assert runner_fingerprint(settings) == runner_fingerprint(relocated)
    assert warmth_identity(settings, warmth_profile="cpu-fake") == warmth_identity(
        relocated, warmth_profile="cpu-fake"
    )
    assert warmth_identity(settings, warmth_profile="cpu-fake") != warmth_identity(
        settings, warmth_profile="cpu-fake-v2"
    )
    assert runner_fingerprint(settings) == runner_fingerprint(settings)


def test_resident_key_excludes_task_identity_but_tracks_artifacts_and_policy(
    settings: dict[str, object],
) -> None:
    task_variant = dict(settings)
    task_variant.update(
        {
            "prompt": "a different scene",
            "seed": 99,
            "resolution": "640x360",
            "video_length": 17,
            "force_fps": "24",
            "num_inference_steps": 12,
            "guidance_scale": 7.5,
            "attempt_root": "/different/private/spool",
        }
    )
    assert resident_key(settings) == resident_key(task_variant)
    assert runner_fingerprint(settings) != runner_fingerprint(task_variant)

    model_variant = dict(task_variant)
    model_variant["model_artifact_digest"] = "sha256:model-a"
    assert resident_key(settings) != resident_key(model_variant)

    policy_variant = dict(settings)
    policy_variant["attention_backend"] = "flash-attn"
    assert resident_key(settings) != resident_key(policy_variant)

    assert resident_key(settings, warmth_profile="cpu-fake") != resident_key(
        settings, warmth_profile="cpu-fake-v2"
    )

    lora_variant = dict(settings)
    lora_variant.update(
        {
            "lora_artifact_digests": ["sha256:lora-a"],
            "lora_activation": "fused",
        }
    )
    assert resident_key(settings) != resident_key(lora_variant)

    with pytest.raises(ValueError, match="unclassified settings"):
        resident_key({**settings, "unknown_retained_switch": True})


def test_fake_output_containment_rejects_escape_without_writing_outside(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    state_path = tmp_path / "runner-state.jsonl"
    output_root = tmp_path / "outputs"
    runner = FakePersistentRunner(state_path, output_root=output_root)

    result = runner.run(settings, escape_output=True)

    escaped = tmp_path / "escaped-fake-output.json"
    assert result.status == "failed"
    assert result.containment_ok is False
    assert result.generated_files == []
    assert "output containment violated" in result.errors[0]
    assert escaped.exists() is False
    assert result.runner_alive is True
    assert runner.state.snapshot.failed_runs == 1


def test_generate_core_refuses_direct_execution_for_valid_inputs() -> None:
    """Direct execution is disabled: valid inputs fail closed to the host-owned session.

    Legacy command-kind contract on the run.py module, which exists until the
    integrator deletes it after parity. The session-path equivalent (invalid
    inputs never submit or cancel) lives in
    tests/packs/wan2gp/test_session_action_skeleton.py.
    """
    from astrid.packs.wan2gp.actions.generate_video.run import build_parser, generate_core

    args = build_parser().parse_args(
        [
            "--prompt", "a kite", "--resolution", "512x512", "--frames", "9",
            "--fps", "8", "--seed", "7", "--guidance-scale", "5.0", "--steps", "4",
        ]
    )
    assert generate_core(args) == (
        2,
        {
            "ok": False,
            "error": "wan2gp.generate_video requires the GenericPackHost-owned Wan session",
            "code": "host_session_required",
        },
    )


_HOST_INPUTS: dict[str, object] = {
    "prompt": "a kite",
    "model": "wan-2.2",
    "resolution": "512x512",
    "frames": 9,
    "fps": 8,
    "seed": 7,
}


def _terminal_evidence(source_root: Path, files: list[Path]) -> dict[str, object]:
    from astrid.packs.wan2gp.src.driver import snapshot_native_outputs

    generated = [str(path) for path in files]
    return {
        "result": {"success": True, "errors": [], "generated_files": generated},
        "terminal": True,
        "native_job_id": "native-job-1",
        "source_root": str(source_root),
        "output_snapshots": snapshot_native_outputs(generated, source_root) if generated else [],
        "events": [],
    }


def test_materialize_host_result_fail_closes_success_without_files(tmp_path: Path) -> None:
    from astrid.packs.wan2gp.src.driver import materialize_host_result

    source_root = tmp_path / "native"
    source_root.mkdir()
    attempt_root = tmp_path / "attempt"

    with pytest.raises(RuntimeError, match="lacks terminal file observations"):
        materialize_host_result(
            _terminal_evidence(source_root, []),
            attempt_root=attempt_root,
            inputs=_HOST_INPUTS,
        )

    assert not attempt_root.exists()


def test_materialize_host_result_receipt_identifies_video_collection(tmp_path: Path) -> None:
    from astrid.packs.wan2gp.src.driver import materialize_host_result

    source_root = tmp_path / "native"
    source_root.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "reshape" / "hype_regression" / "main.mp4"
    clips = [source_root / "first.mp4", source_root / "second.mp4"]
    for clip in clips:
        shutil.copyfile(fixture, clip)
    attempt_root = tmp_path / "attempt"

    mapped = materialize_host_result(
        _terminal_evidence(source_root, clips),
        attempt_root=attempt_root,
        inputs=_HOST_INPUTS,
    )

    outputs = mapped["manifest"]["outputs"]
    assert mapped["generated_files"] == ["first.mp4", "second.mp4"]
    assert [entry["name"] for entry in outputs] == ["generated_videos", "generated_videos"]
    assert [entry["ordinal"] for entry in outputs] == [0, 1]
    assert [entry["role"] for entry in outputs] == ["result", "result"]
    assert [entry["is_primary"] for entry in outputs] == [True, False]
    assert all(entry["content_hash"].startswith("sha256:") for entry in outputs)
    assert all(entry["bytes"] > 0 for entry in outputs)
    expected_hash = "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert [entry["content_hash"] for entry in outputs] == [expected_hash, expected_hash]
    assert (attempt_root / "first.mp4").read_bytes() == fixture.read_bytes()
    assert (attempt_root / "manifest.json").is_file()


@pytest.mark.integration  # needs mutagen, which is not a dev dependency
def test_generated_mp4_wall_clock_comments_canonicalize_to_equal_digest(
    tmp_path: Path,
) -> None:
    from mutagen import mp4 as mutagen_mp4

    source = Path(__file__).parent / "fixtures" / "reshape" / "hype_regression" / "main.mp4"
    cold = tmp_path / "cold.mp4"
    warm = tmp_path / "warm.mp4"
    shutil.copyfile(source, cold)
    shutil.copyfile(source, warm)

    timestamps = ("2026-09-08T10:00:00.000000Z", "2026-09-08T10:00:01.000000Z")
    for path, timestamp in zip((cold, warm), timestamps, strict=True):
        media = mutagen_mp4.MP4(path)
        if media.tags is None:
            media.add_tags()
        media.tags["©cmt"] = [
            json.dumps(
                {
                    "creation_date": timestamp,
                    "creation_timestamp": timestamp,
                    "generation_time": timestamp,
                    "prompt": "fixed prompt",
                }
            )
        ]
        media.save()

    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (cold, warm)]
    assert before[0] != before[1]

    _canonicalize_generated_files([str(cold), str(warm)])

    after = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (cold, warm)]
    assert after[0] == after[1]
    assert mutagen_mp4.MP4(cold).tags["©cmt"] == ['{"prompt":"fixed prompt"}']
