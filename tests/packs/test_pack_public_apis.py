from astrid.packs.rendering import api as rendering_api
from astrid.packs.rendering.actions.timeline_visualize.layers import base as layers_base
from astrid.packs.rendering.shared import runtime_stitch
from astrid.packs.vibecomfy import api as vibecomfy_api
from astrid.packs.vibecomfy import asset_manifest


def test_vibecomfy_api_reexports_asset_manifest_contract():
    for name in vibecomfy_api.__all__:
        assert getattr(vibecomfy_api, name) is getattr(asset_manifest, name)


def test_rendering_api_reexports_layer_and_stitch_contracts():
    for name in (
        "Check",
        "CheckContext",
        "Finding",
        "Layer",
        "LayerContext",
        "LayerResult",
        "PALETTE",
        "draw_text",
        "text_width",
    ):
        assert getattr(rendering_api, name) is getattr(layers_base, name)
    for name in ("build_stitch_admission", "publish_authoring_proposal"):
        assert getattr(rendering_api, name) is getattr(runtime_stitch, name)


def test_pack_and_sdk_facades_expose_pipeline_and_media_contracts():
    from astrid import pack_api, sdk
    from astrid.core.execution import pipeline_step
    from astrid.core.util import llm_clients
    from astrid.sdk import capability_selection, media_transcript, timeline_intent

    assert pack_api.GeminiClient is llm_clients.GeminiClient
    assert pack_api.build_gemini_client is llm_clients.build_gemini_client
    assert pack_api.run_step is pipeline_step.run_step
    assert pack_api.redact_pipeline_command is pipeline_step._redact_command
    assert sdk.MEDIA_TRANSCRIPT_SCHEMA == media_transcript.SCHEMA
    assert sdk.WHISPER_SOURCE == media_transcript.WHISPER_SOURCE
    assert sdk.write_transcript is media_transcript.write_transcript
    assert sdk.timeline_intent_words is timeline_intent.words
    assert sdk.select_capability_row is capability_selection.select_capability_row


def test_moving_packs_can_access_existing_media_and_timeline_contracts():
    from astrid import pack_api, sdk
    from astrid.core import env_vars, media
    from astrid.core.rendering import remotion_runtime
    from astrid.core.timeline import audio_curves, clip_fields
    from astrid.sdk import media_handles, timeline

    assert pack_api.clip_fields is clip_fields
    assert pack_api.audio_curve_registry is audio_curves.registry
    assert pack_api.ASTRID_REMOTION_PROJECT_DIR == env_vars.ASTRID_REMOTION_PROJECT_DIR
    assert pack_api.resolve_remotion_project_dir is remotion_runtime.resolve_remotion_project_dir
    assert pack_api.load_rgba is media.load_rgba
    assert pack_api.outline_integrity is media.outline_integrity
    assert sdk.resolve_media_handle is media_handles.resolve_media_handle
    assert sdk.save_media is media_handles.save_media
    assert sdk.Checkout is timeline.Checkout


def test_runtime_custody_helpers_are_real_and_work(tmp_path):
    import os
    import subprocess
    import sys
    from astrid import pack_api
    from runtime_protocol import platform

    assert pack_api.owner_only is platform.owner_only
    assert pack_api.spawn_tree is platform.spawn_tree
    token = tmp_path / "token"
    token.write_text("test")
    pack_api.owner_only(token)
    if os.name != "nt":
        assert token.stat().st_mode & 0o777 == 0o600
    child = pack_api.spawn_tree(
        [sys.executable, "-c", "print('spawned')"],
        detached=True, stdout=subprocess.PIPE, text=True,
    )
    stdout, _ = child.communicate(timeout=10)
    assert child.returncode == 0 and stdout.strip() == "spawned"


def test_runtime_dirfd_filesystem_is_the_runtime_object(tmp_path):
    from astrid import pack_api
    from runtime_protocol.dirfd import filesystem

    fs = pack_api.runtime_dirfd_filesystem()
    assert fs is filesystem
    directory_fd = fs.open(tmp_path, fs.O_RDONLY | getattr(fs, "O_DIRECTORY", 0))
    try:
        fd = fs.open("proof", fs.O_CREAT | fs.O_WRONLY | fs.O_EXCL, 0o600, dir_fd=directory_fd)
        fs.close(fd)
        assert fs.stat("proof", dir_fd=directory_fd).st_size == 0
    finally:
        fs.close(directory_fd)
