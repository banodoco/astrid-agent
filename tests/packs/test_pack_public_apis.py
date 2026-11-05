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
