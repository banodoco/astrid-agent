"""Pack authors can use the domain APIs without importing implementation paths."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_external_pack_imports_only_domain_public_surfaces() -> None:
    code = """
from astrid.packs.vibecomfy.api import ImageRequest, compile_image_request
from astrid.packs.rendering.api import RenderContext, RenderRequest, renderer_main, support

compiled = compile_image_request("z_image_turbo", {"prompt": "hello"}, profile="pip_embedded")
assert compiled.capability_id == "z_image_turbo"
assert ImageRequest is not None
assert RenderContext is not None and RenderRequest is not None
assert callable(renderer_main) and callable(support)
"""
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def test_domain_apis_are_explicit_and_versioned() -> None:
    from astrid.packs.rendering import api as rendering_api
    from astrid.packs.vibecomfy import api as vibecomfy_api

    for api in (rendering_api, vibecomfy_api):
        assert api.VERSION == (1, 0)
        assert len(api.__all__) == len(set(api.__all__))
        assert set(api.__all__) == set(api._EXPORTS) | {"VERSION"}
        assert "_EXPORTS" not in api.__all__

    # The public rendering API intentionally does not grant direct product
    # rendering authority; callers admit that operation through the runtime.
    assert "render" not in rendering_api.__all__
