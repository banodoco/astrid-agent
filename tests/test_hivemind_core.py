"""Core-bundled Hivemind manifest and import contract."""

from __future__ import annotations

import importlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "astrid/core/hivemind/pack.yaml"
OPERATIONS = {
    "search", "get_item", "refresh_media", "contribute", "ingest_article",
    "ingest_workflow", "ingest_youtube",
}
WRITE_OPERATIONS = {"contribute", "ingest_article", "ingest_workflow", "ingest_youtube"}


def test_all_seven_hivemind_operations_resolve_to_core_implementation() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert set(manifest["actions"]) == OPERATIONS
    assert set(manifest["agent"]["normal_entrypoints"]) == {f"hivemind.{name}" for name in OPERATIONS}
    for action in manifest["actions"].values():
        module = action["metadata"]["runtime_module"]
        assert module.startswith("astrid.core.hivemind.executors.")
        assert importlib.import_module(module)


def test_hivemind_manifest_uses_core_requirements_and_action_secrets() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["dependencies"] == {"packs": [], "python_requirements": []}
    for name, action in manifest["actions"].items():
        assert "secrets_required" not in action.get("isolation", {})
        assert action.get("secrets", []) == (
            ["HIVEMIND_CONTRIBUTOR_KEY"] if name in WRITE_OPERATIONS else []
        )


def test_contributor_key_is_read_from_injected_environment_only(monkeypatch, tmp_path: Path) -> None:
    from astrid.core.hivemind.executors._common import resolve_contributor_key

    monkeypatch.delenv("HIVEMIND_CONTRIBUTOR_KEY", raising=False)
    key_file = tmp_path / ".hivemind" / "key"
    key_file.parent.mkdir()
    key_file.write_text("hm_" + "c" * 64, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_contributor_key() is None

    monkeypatch.setenv("HIVEMIND_CONTRIBUTOR_KEY", "injected-value")
    assert resolve_contributor_key() == "injected-value"
