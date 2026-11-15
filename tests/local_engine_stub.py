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


def prepared_local_engine(monkeypatch, url: str = LOCAL_ENGINE_URL) -> None:
    """Stand in for owned preparation of the installed engine, for tests of what runs after it.

    A local managed run first prepares the engine under custody, then runs once against the
    prepared endpoint. This replaces only that preparation: the real run executes against
    ``url`` with a prepared attempt, so the producer and settlement under test are unchanged.
    """
    import astrid.core.generation.managed_preparation as managed
    from astrid.packs.vibecomfy import local_job
    from astrid.packs.vibecomfy.actions.run import run as run_action

    def run_owned_action(workflow, out, *, task_identity, attempt_identity, align,
                         expected_execution_identity=None, **kwargs):
        return run_action._run_and_settle(workflow, out, task_identity=task_identity,
                                          attempt_identity=attempt_identity, align=align,
                                          comfy_url=url, **kwargs)

    def prepared_attempt(*, endpoint, execution_identity=None, **_kwargs):
        if endpoint != url:
            return None
        return {"placement": "managed", "alignment": {"mode": "check"}, "manifest": {"models": []},
                "execution_identity": execution_identity}

    monkeypatch.setattr(local_job, "run_owned_action", run_owned_action)
    monkeypatch.setattr(managed, "read_preparation", prepared_attempt)
