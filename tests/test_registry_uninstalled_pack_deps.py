"""Dependencies on executors of packs that are not installed.

Moved packs (editorial, understanding.scene_describe) live in poms_packs.  A
core-only install must still load, list and describe; the dependents are
reported unavailable.  A typo inside an installed pack must still raise.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from astrid.core.execution.executor.registry import (
    ExecutorRegistry,
    ExecutorRegistryError,
    load_default_registry,
)

SIX_DEFAULT_ENTRIES = (
    "qwen-image-2.1-turbo",
    "qwen-image-2.1-turbo-edit",
    "h3",
    "audio/qwen3_tts_custom_voice",
    "audio/minimax_music_3",
    "audio/hunyuan_video_foley",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "astrid", "generate", *args],
        capture_output=True, text=True, timeout=120,
    )


def _definition(executor_id: str, depends_on: tuple[str, ...] = ()) -> dict:
    return {
        "id": executor_id,
        "name": executor_id,
        "kind": "built_in",
        "version": "1",
        "command": {"argv": ["true"]},
        "graph": {"depends_on": list(depends_on)},
    }


def test_core_only_registry_marks_pool_build_unavailable_naming_editorial() -> None:
    registry = load_default_registry()
    for executor_id in ("training.pool_build", "training.pool_merge"):
        definition = registry.get(executor_id)
        assert definition.metadata.get("available") is False
        reason = definition.metadata["unavailable_reason"]
        assert "editorial" in reason and "poms_packs" in reason
        assert registry.unavailable_reason(executor_id) == reason


def test_core_only_generate_list_works() -> None:
    result = _run("list", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    json.loads(result.stdout)


@pytest.mark.parametrize("entry", SIX_DEFAULT_ENTRIES)
def test_core_only_generate_describe_is_not_blocked(entry: str) -> None:
    result = _run("describe", "--entry", entry, "--json")
    assert "depends on unknown executor" not in result.stdout + result.stderr


def test_core_only_describe_pool_build_is_not_blocked() -> None:
    result = _run("describe", "--entry", "training.pool_build", "--json")
    assert "depends on unknown executor" not in result.stdout + result.stderr


def test_typo_inside_installed_pack_still_raises() -> None:
    registry = ExecutorRegistry([_definition("alpha.one", ("alpha.typo",))])
    registry.installed_namespaces = {"alpha"}
    with pytest.raises(ExecutorRegistryError, match="unknown executor 'alpha.typo'"):
        registry.validate_all()


def test_missing_namespace_marks_unavailable_without_raising() -> None:
    registry = ExecutorRegistry([_definition("alpha.one", ("gone.thing",))])
    registry.installed_namespaces = {"alpha"}
    registry.validate_all()
    assert "gone" in registry.unavailable_reason("alpha.one")


def test_registry_without_namespace_knowledge_stays_strict() -> None:
    registry = ExecutorRegistry([_definition("alpha.one", ("gone.thing",))])
    with pytest.raises(ExecutorRegistryError):
        registry.validate_all()
