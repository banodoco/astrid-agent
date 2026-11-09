"""The engine-side facts this pack needs, installed by its host instead of imported.

Nothing under ``h3_av`` outside ``actions/*/run.py`` imports Astrid. The host (the
comfyui engine for the ``vibecomfy.workflows`` hooks, or a stage's ``run.py``) calls
``install`` with an object carrying: ``comfy_commit()``, ``locked_models()``,
``bind_model_root(model_root)``, ``build_asset_manifest(...)``, ``read_archive(path)``,
``write_json_atomic(path, value)``, ``assert_no_unresolved_placeholders(bindings)`` and
``measure_source_av(path)``.
"""
from __future__ import annotations

from typing import Any

_FACTS: Any = None


def install(facts: Any) -> None:
    global _FACTS
    _FACTS = facts


def facts() -> Any:
    if _FACTS is None:
        raise RuntimeError("h3_av engine facts are not installed; the host must call shared.engine_facts.install")
    return _FACTS
