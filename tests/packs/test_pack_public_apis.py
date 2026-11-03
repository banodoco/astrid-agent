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
