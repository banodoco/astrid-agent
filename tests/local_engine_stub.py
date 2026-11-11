"""Test support: the installed local ComfyUI engine as a fixed loopback endpoint (no server, no install)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

LOCAL_ENGINE_URL = "http://127.0.0.1:8188"


def local_engine_patch(url: str = LOCAL_ENGINE_URL, comfy_output_dir: str = ""):
    """Patch local_engine_endpoint, which ComfyUIEngine.run's local resolves at the run site."""
    return patch(
        "astrid.core.generation.engine_local_endpoint.local_engine_endpoint",
        return_value=SimpleNamespace(endpoint=SimpleNamespace(url=url), comfy_output_dir=comfy_output_dir,
                                     model_root_binding=None),
    )
