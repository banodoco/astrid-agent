from __future__ import annotations

import pytest

from astrid.packs.vibecomfy.media import (
    CharacterAnimationRequest,
    DirectVibeMediaExecutor,
    MediaCompileError,
    MediaExecutionError,
    VideoEnhanceRequest,
    WanI2VRequest,
    WanT2IRequest,
    compile_character_animation,
    compile_video_enhance,
    compile_wan_2_2_i2v,
    compile_wan_2_2_t2i,
    profile_semantics,
)


@pytest.mark.parametrize("profile", ["local_server", "checkout_server"])
def test_direct_wan_capabilities_are_typed_and_profile_bound(profile: str) -> None:
    t2i = compile_wan_2_2_t2i(
        WanT2IRequest(prompt="a red kite over a lake"), profile=profile
    )
    i2v = compile_wan_2_2_i2v(
        WanI2VRequest(image_ref="media://start", prompt="the kite moves"),
        profile=profile,
    )

    assert t2i.capability_id == "vibecomfy.wan_2_2_t2i"
    assert t2i.model_identity == "wan-2.2-a14b-t2v"
    assert t2i.inputs["frames"] == 1
    assert t2i.workflow["nodes"]["latent"]["inputs"]["frames"] == 1
    assert t2i.template_id != "wan2gp.generate_video"
    assert i2v.capability_id == "vibecomfy.wan_2_2_i2v"
    assert i2v.model_identity == "wan-2.2-a14b-i2v"
    assert i2v.workflow["nodes"]["image"]["inputs"]["image"] == "media://start"
    assert i2v.profile.profile_id == profile


def test_profile_semantics_are_explicit_and_distinct() -> None:
    embedded = profile_semantics("local_server")
    checkout = profile_semantics("checkout_server")

    assert embedded.engine_identity != checkout.engine_identity
    assert embedded.lifecycle == "external_server"
    assert checkout.lifecycle == "managed_checkout_session"
    assert "runtime_instance_id" not in embedded.required_runtime_fields
    assert "runtime_instance_id" in checkout.required_runtime_fields
    assert embedded.output_transport != checkout.output_transport


def _runtime_context(profile: str) -> dict[str, str | int]:
    if profile == "local_server":
        return {
            "profile_digest": "sha256:" + "a" * 64,
            "model_bytes_digest": "sha256:" + "b" * 64,
        }
    return {
        "profile_digest": "sha256:" + "a" * 64,
        "runtime_instance_id": "runtime-1",
        "environment_fingerprint": "env-1",
        "model_bytes_digest": "sha256:" + "b" * 64,
        "declared_root": "/worker/vibecomfy",
        "declared_port": 8188,
    }


@pytest.mark.parametrize("profile", ["local_server", "checkout_server"])
@pytest.mark.parametrize(
    "media_request",
    [
        VideoEnhanceRequest(
            video_ref="media://source",
            enable_interpolation=True,
        ),
        VideoEnhanceRequest(
            video_ref="media://source",
            color_fix=True,
        ),
        VideoEnhanceRequest(
            video_ref="media://source",
            output_quality="high",
        ),
        VideoEnhanceRequest(
            video_ref="media://source",
            interpolation_frames=2,
        ),
        VideoEnhanceRequest(
            video_ref="media://source",
            enable_upscale=False,
        ),
    ],
)
def test_video_enhance_unsupported_controls_fail_before_runner(
    profile: str, media_request: VideoEnhanceRequest
) -> None:
    seen: list[object] = []

    def runner(*args, **kwargs):
        seen.append((args, kwargs))
        return {"artifact": "should-not-exist"}

    executor = DirectVibeMediaExecutor(profile, runner)
    with pytest.raises(MediaExecutionError, match="unsupported|requires enable_upscale"):
        executor.execute_video_enhance(
            media_request,
            task_identity="task-video",
            runtime_context=_runtime_context(profile),
        )
    assert seen == []


@pytest.mark.parametrize("profile", ["local_server", "checkout_server"])
def test_video_enhance_bounded_profile_binds_only_canonical_inputs(profile: str) -> None:
    seen: list[object] = []

    def runner(workflow, **kwargs):
        seen.append((workflow, kwargs))
        return {"artifact": "media://output"}

    result = DirectVibeMediaExecutor(profile, runner).execute_video_enhance(
        VideoEnhanceRequest(video_ref="media://source"),
        task_identity="task-video",
        runtime_context=_runtime_context(profile),
    )

    assert result.capability_id == "vibecomfy.video_enhance"
    assert len(seen) == 1
    workflow, _kwargs = seen[0]
    assert workflow["template_id"] == "video/basic_video_enhance"
    assert workflow["bindings"] == {
        "video_ref": "media://source",
        "scale": 2.0,
        "upscale_method": "lanczos",
    }


@pytest.mark.parametrize("profile", ["local_server", "checkout_server"])
def test_character_animation_replacement_mode_fails_before_runner(profile: str) -> None:
    seen: list[object] = []

    def runner(*args, **kwargs):
        seen.append((args, kwargs))
        return {"artifact": "should-not-exist"}

    executor = DirectVibeMediaExecutor(profile, runner)
    with pytest.raises(MediaExecutionError, match="mode is unsupported"):
        executor.execute_character_animation(
            CharacterAnimationRequest(
                reference_image_ref="media://character",
                driving_video_ref="media://motion",
                mode="replace",
                resolution="480p",
                prompt="walk forward",
            ),
            task_identity="task-character",
            runtime_context=_runtime_context(profile),
        )
    assert seen == []


@pytest.mark.parametrize("profile", ["local_server", "checkout_server"])
def test_character_animation_bounded_profile_binds_two_media_inputs(profile: str) -> None:
    seen: list[object] = []

    def runner(workflow, **kwargs):
        seen.append((workflow, kwargs))
        return {"artifact": "media://output"}

    result = DirectVibeMediaExecutor(profile, runner).execute_character_animation(
        CharacterAnimationRequest(
            reference_image_ref="media://character",
            driving_video_ref="media://motion",
            mode="animate",
            resolution="720p",
            prompt="walk forward",
            negative_prompt="blurry",
            seed=7,
            frames=41,
            fps=24,
            steps=4,
        ),
        task_identity="task-character",
        runtime_context=_runtime_context(profile),
    )

    assert result.capability_id == "vibecomfy.character_animation"
    workflow, _kwargs = seen[0]
    assert workflow["template_id"] == "video/wan22_animate_native_first_stage"
    assert workflow["bindings"] == {
        "input_image": "media://character",
        "driving_video": "media://motion",
        "prompt": "walk forward",
        "negative_prompt": "blurry",
        "seed": 7,
        "width": 1280,
        "height": 720,
        "frames": 41,
        "fps": 24,
        "steps": 4,
    }


def test_video_enhance_control_rejection_precedes_request_validation() -> None:
    with pytest.raises(MediaCompileError, match="enable_interpolation is unsupported"):
        compile_video_enhance(
            VideoEnhanceRequest(
                video_ref="",
                enable_interpolation=True,
            )
        )


def test_video_enhance_cli_defaults_upscale_on_and_removes_interpolation_option() -> None:
    from astrid.packs.vibecomfy.media.runtime import build_parser

    parser = build_parser("vibecomfy.video_enhance")
    args = parser.parse_args(
        [
            "--capability", "vibecomfy.video_enhance",
            "--out", "/tmp/out",
            "--task-identity", "task-video",
            "--profile", "local_server",
        ]
    )
    assert args.enable_upscale is True
    assert not hasattr(args, "enable_interpolation")
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--capability", "vibecomfy.video_enhance",
                "--out", "/tmp/out",
                "--task-identity", "task-video",
                "--profile", "local_server",
                "--enable-interpolation", "true",
            ]
        )


def test_media_command_rejects_before_profile_read_or_scratch_creation(tmp_path) -> None:
    from astrid.packs.vibecomfy.media import runtime as run

    output = tmp_path / "attempt-output"
    with pytest.raises(MediaCompileError, match="enable_interpolation is unsupported"):
        run._run(
            capability="vibecomfy.video_enhance",
            request=VideoEnhanceRequest(video_ref="media://source", enable_interpolation=True),
            task_identity="task-video",
            profile_id="local_server",
            readiness_profile_path=str(tmp_path / "missing-profile.json"),
            readiness_profile_hash="sha256:" + "a" * 64,
            out=output,
        )
    assert not output.exists()


def test_typed_media_output_enforces_runtime_per_object_limit(tmp_path, monkeypatch) -> None:
    from astrid.packs.vibecomfy.media import runtime as run

    output = tmp_path / "output.mp4"
    output.write_bytes(b"12345")
    monkeypatch.setattr(run, "RUNTIME_OBJECT_MAX_BYTES", 4)
    with pytest.raises(RuntimeError, match="per-object limit"):
        run._assert_runtime_object_size(output, label="typed Vibe output")


def test_compilation_is_deterministic_and_rejects_native_wan_confusion() -> None:
    request = WanT2IRequest(prompt="a quiet lake")
    first = compile_wan_2_2_t2i(request)
    second = compile_wan_2_2_t2i(request)

    assert first.to_dict() == second.to_dict()
    assert first.request_digest.startswith("sha256:")
    assert first.capability_id != "wan2gp.generate_video"

    with pytest.raises(MediaCompileError, match="unsupported Vibe profile"):
        compile_wan_2_2_t2i(request, profile="wan2gp")


def test_executor_requires_profile_identity_and_does_not_fallback() -> None:
    seen: list[tuple[str, str, str]] = []

    def runner(workflow, *, capability_id, model_identity, profile_id, task_identity):
        seen.append((capability_id, model_identity, profile_id))
        assert workflow["nodes"]
        return {"artifact": "media://output"}

    executor = DirectVibeMediaExecutor("checkout_server", runner)
    request = WanT2IRequest(prompt="a quiet lake")
    with pytest.raises(MediaExecutionError, match="runtime identity is incomplete"):
        executor.execute_wan_2_2_t2i(
            request,
            task_identity="task-1",
            runtime_context={"model_bytes_digest": "sha256:" + "a" * 64},
        )
    assert seen == []

    result = executor.execute_wan_2_2_t2i(
        request,
        task_identity="task-1",
        runtime_context={
            "profile_digest": "sha256:" + "b" * 64,
            "runtime_instance_id": "runtime-1",
            "environment_fingerprint": "env-1",
            "model_bytes_digest": "sha256:" + "a" * 64,
            "declared_root": "/worker/vibecomfy",
            "declared_port": 8188,
        },
    )
    assert result.profile_id == "checkout_server"
    assert result.output_transport == "checkout_server_publication"
    assert result.outputs == {"artifact": "media://output"}
    assert seen == [("vibecomfy.wan_2_2_t2i", "wan-2.2-a14b-t2v", "checkout_server")]
